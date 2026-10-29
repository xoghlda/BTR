"""Publish explicit GitHub assets through a verified, resumable draft release.

Default dry-run is entirely local. --preflight reads GitHub; --publish mutates
only the requested matching draft. Credentials remain in this process memory.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from urllib.parse import quote, urlencode, urljoin, urlsplit

API_VERSION = "2026-03-10"
AUTH_HOSTS = {"api.github.com", "uploads.github.com"}
DOWNLOAD_HOSTS = AUTH_HOSTS | {
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
CHUNK = 1024 * 1024


class PublishError(Exception):
    pass


class ApiError(PublishError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub returned HTTP {status}. No response body or credential is logged.")


def checked_url(url: str, allowed_hosts: set[str]):
    if any(ord(character) < 33 or ord(character) == 127 for character in url):
        raise PublishError("Unsafe URL characters.")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in allowed_hosts
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise PublishError("Refusing a non-approved HTTPS endpoint.")
    return parsed


def credential_from_git() -> str:
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GCM_GUI_PROMPT="false")
    try:
        response = subprocess.run(
            ["git", "-c", "credential.interactive=false", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n", text=True, encoding="utf-8",
            capture_output=True, env=environment, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise PublishError("Existing GitHub credentials could not be read non-interactively.") from None
    if response.returncode == 0:
        for line in response.stdout.splitlines():
            if line.startswith("password=") and line[9:]:
                return line[9:]
    raise PublishError("No existing GitHub credential is available; no credential was printed or saved.")


@dataclass(frozen=True)
class Asset:
    path: Path
    name: str
    size: int
    sha256: str
    mtime_ns: int


def local_assets(paths: list[str]) -> list[Asset]:
    result = []
    seen = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise PublishError("Every asset must be an existing, non-empty regular file.")
        if any(ord(character) < 32 or ord(character) == 127 for character in path.name):
            raise PublishError("Asset names must not contain control characters.")
        if path.name.casefold() in seen:
            raise PublishError("Asset filenames must be unique, including case differences.")
        seen.add(path.name.casefold())
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                digest.update(chunk)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PublishError("An asset changed while being hashed; freeze build outputs first.")
        result.append(Asset(path, path.name, stat.st_size, digest.hexdigest(), stat.st_mtime_ns))
    return result


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self._token = token
        self.base = f"/repos/{repo}"

    def _connect(self, method: str, url: str, *, auth: bool, headers=None, payload=None, file=None):
        parsed = checked_url(url, AUTH_HOSTS if auth else DOWNLOAD_HOSTS)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=180, context=ssl.create_default_context())
        request_headers = {
            "Accept": "application/vnd.github+json", "User-Agent": "explicit-release-publisher/1",
            "X-GitHub-Api-Version": API_VERSION, "Accept-Encoding": "identity",
        }
        if auth:
            request_headers["Authorization"] = f"Bearer {self._token}"
        request_headers.update(headers or {})
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        try:
            connection.request(method, target, body=file if file is not None else body, headers=request_headers)
            return connection, connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
            raise PublishError("GitHub connection failed. Inspect with --preflight before safely rerunning.") from None

    def request(self, method: str, path: str, payload=None, *, upload: Asset | None = None):
        source = None
        try:
            headers = {}
            host = "api.github.com"
            if upload is not None:
                current = upload.path.stat()
                if (current.st_size, current.st_mtime_ns) != (upload.size, upload.mtime_ns):
                    raise PublishError("An asset changed after preflight; upload refused.")
                source = upload.path.open("rb")
                headers = {"Content-Type": "application/octet-stream", "Content-Length": str(upload.size)}
                host = "uploads.github.com"
            connection, response = self._connect(method, f"https://{host}{path}", auth=True, headers=headers, payload=payload, file=source)
            try:
                data = response.read(4 * CHUNK + 1)
                if not 200 <= response.status < 300:
                    raise ApiError(response.status)
                if len(data) > 4 * CHUNK:
                    raise PublishError("GitHub JSON response exceeded the safety limit.")
                return json.loads(data) if data else {}
            finally:
                connection.close()
        except (OSError, http.client.HTTPException, ValueError):
            raise PublishError("GitHub request or local asset read failed; use --preflight before rerunning.") from None
        finally:
            if source is not None:
                source.close()

    def optional(self, path: str):
        try:
            return self.request("GET", path)
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def all_pages(self, path: str):
        result = []
        for page in range(1, 101):
            values = self.request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(values, list):
                raise PublishError("Unexpected GitHub list response.")
            result.extend(values)
            if len(values) < 100:
                return result
        raise PublishError("Pagination limit reached; no write was attempted by this read.")

    def release(self, tag: str):
        matches = [release for release in self.all_pages(self.base + "/releases") if release.get("tag_name") == tag]
        if len(matches) > 1:
            raise PublishError("Several releases use this tag; refusing to choose one.")
        return matches[0] if matches else None

    def tag_target(self, tag: str):
        reference = self.optional(self.base + "/git/ref/tags/" + quote(tag, safe=""))
        if reference is None:
            return None
        obj = reference["object"]
        for _ in range(10):
            if obj.get("type") == "commit":
                return obj["sha"]
            if obj.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", str(obj.get("sha", ""))):
                break
            obj = self.request("GET", self.base + "/git/tags/" + obj["sha"])["object"]
        raise PublishError("Tag cannot be resolved to a single commit.")

    def assets(self, release_id: int):
        return self.all_pages(self.base + f"/releases/{release_id}/assets")

    def asset_hash(self, asset: dict) -> str:
        digest = asset.get("digest", "") or ""
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            return digest[7:].lower()
        url = f"https://api.github.com{self.base}/releases/assets/{int(asset['id'])}"
        authenticated = True
        for _ in range(6):
            connection, response = self._connect("GET", url, auth=authenticated, headers={"Accept": "application/octet-stream"})
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise PublishError("Asset redirect had no Location.")
                    url = urljoin(url, location)
                    checked_url(url, DOWNLOAD_HOSTS)
                    authenticated = False  # Never forward authorization to any redirect.
                    continue
                if response.status != 200:
                    raise ApiError(response.status)
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > int(asset["size"]):
                        raise PublishError("Downloaded asset exceeded its declared size.")
                    digest.update(chunk)
                if size != int(asset["size"]):
                    raise PublishError("Downloaded asset was incomplete.")
                return digest.hexdigest()
            except (OSError, http.client.HTTPException):
                raise PublishError("Asset verification download failed; existing assets were left untouched.") from None
            finally:
                connection.close()
        raise PublishError("Too many asset redirects.")


def compare_assets(client: GitHub, remote: list[dict], assets: list[Asset]) -> list[Asset]:
    names = [item.get("name", "") for item in remote]
    if len({name.casefold() for name in names}) != len(names):
        raise PublishError("Existing release has ambiguous duplicate asset names.")
    expected = {asset.name: asset for asset in assets}
    if any(name not in expected for name in names):
        raise PublishError("Existing release includes assets outside the explicit requested set.")
    for item in remote:
        asset = expected[item["name"]]
        if item.get("state") != "uploaded" or item.get("size") != asset.size or client.asset_hash(item) != asset.sha256:
            raise PublishError(f"Existing asset conflicts with the local file: {asset.name}. Nothing is replaced or deleted.")
    return [asset for asset in assets if asset.name not in names]


def inspect_remote(client: GitHub, args, assets: list[Asset], body: str):
    repository = client.request("GET", client.base)
    if repository.get("full_name", "").casefold() != args.repo.casefold():
        raise PublishError("Repository identity did not match the explicit repository.")
    commit = client.request("GET", client.base + "/git/commits/" + args.target)
    if commit.get("sha") != args.target:
        raise PublishError("The explicit target commit is not present in this GitHub repository.")
    tag_target = client.tag_target(args.tag)
    if tag_target is not None and tag_target != args.target:
        raise PublishError("Existing remote tag points to another commit.")
    release = client.release(args.tag)
    if release is None:
        return None, assets
    if (release.get("name") != args.title or (release.get("body") or "").replace("\r\n", "\n") != body
            or bool(release.get("prerelease")) != args.prerelease):
        raise PublishError("Existing release title/body/prerelease conflicts with the explicit requested release.")
    if tag_target is None and (not release.get("draft") or release.get("target_commitish") != args.target):
        raise PublishError("Existing release target cannot be verified against the explicit full commit.")
    missing = compare_assets(client, client.assets(int(release["id"])), assets)
    if missing and not release.get("draft"):
        raise PublishError("Published release is missing requested assets; published releases are never changed.")
    return release, missing


def parser():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--repo", required=True, help="Explicit owner/repository")
    arguments.add_argument("--tag", required=True)
    arguments.add_argument("--target", required=True, help="Full 40-character commit already pushed to GitHub")
    arguments.add_argument("--title", required=True)
    arguments.add_argument("--body-file", required=True, type=Path)
    arguments.add_argument("--asset", action="append", required=True, help="One explicit existing file; repeat for every asset")
    arguments.add_argument("--prerelease", action="store_true")
    modes = arguments.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Default: local hashing only; no credential or network access")
    modes.add_argument("--preflight", action="store_true", help="Authenticated read-only remote verification")
    modes.add_argument("--publish", action="store_true", help="Create/resume matching draft, verify uploads, then publish")
    return arguments


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
            raise PublishError("Repository must be an explicit owner/name.")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", args.target):
            raise PublishError("Target must be a full 40-character commit SHA.")
        args.target = args.target.lower()
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", args.tag) or ".." in args.tag
                or "//" in args.tag or args.tag.endswith(("/", ".")) or any(part.endswith(".lock") for part in args.tag.split("/"))):
            raise PublishError("Unsupported or unsafe tag name.")
        body = args.body_file.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        assets = local_assets(args.asset)
        summary = {"repo": args.repo, "tag": args.tag, "target": args.target, "title": args.title,
                   "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                   "assets": [{"name": asset.name, "bytes": asset.size, "sha256": asset.sha256} for asset in assets]}
        if not args.preflight and not args.publish:
            print(json.dumps({"mode": "dry-run", "network": False, "plan": "Create/resume matching draft, verify all assets, publish", **summary}, ensure_ascii=True))
            return 0
        client = GitHub(args.repo, credential_from_git())
        try:
            release, missing = inspect_remote(client, args, assets, body)
            if args.preflight:
                print(json.dumps({"mode": "preflight", "writes": False,
                    "release": "new draft planned" if release is None else ("matching draft" if release.get("draft") else "matching published release"),
                    "missing_assets": [asset.name for asset in missing], **summary}, ensure_ascii=True))
                return 0
            if release is None:
                release = client.request("POST", client.base + "/releases", {
                    "tag_name": args.tag, "target_commitish": args.target, "name": args.title,
                    "body": body, "draft": True, "prerelease": args.prerelease,
                })
            if release.get("draft"):
                for asset in missing:
                    path = client.base + f"/releases/{int(release['id'])}/assets?" + urlencode({"name": asset.name})
                    uploaded = client.request("POST", path, upload=asset)
                    compare_assets(client, [uploaded], [asset])
                # Read back metadata, tag and every asset before making anything public.
                release, remaining = inspect_remote(client, args, assets, body)
                if release is None or remaining:
                    raise PublishError("Draft is not fully verified; it was left unpublished.")
                if release.get("draft"):
                    release = client.request("PATCH", client.base + f"/releases/{int(release['id'])}", {"draft": False, "make_latest": "true"})
            if release.get("draft"):
                raise PublishError("GitHub did not confirm publication; inspect before rerunning.")
            print(json.dumps({"mode": "published", "url": f"https://github.com/{args.repo}/releases/tag/{quote(args.tag, safe='')}", **summary}, ensure_ascii=True))
            return 0
        finally:
            client._token = ""
    except PublishError as error:
        print(f"Release operation refused: {error}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("Release operation failed safely. No credential or raw server response is logged; inspect with --preflight before rerunning.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
