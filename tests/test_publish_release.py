"""Offline release safety checks: no credential helper or network is invoked."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import publish_release as release


class ReleasePublisherTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "artifacts" / "test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / "program.exe"
        self.file.write_bytes(b"development fixture binary")
        self.notes = self.root / "notes.txt"
        self.notes.write_text("Fixture release notes\n", encoding="utf-8")
        self.asset = release.local_assets([str(self.file)])[0]
        self.args = SimpleNamespace(repo="owner/repository", tag="v1.0.0", target="a" * 40,
                                    title="Fixture release", prerelease=False)
        self.body = self.notes.read_text(encoding="utf-8")

    def cli(self):
        return ["--repo", self.args.repo, "--tag", self.args.tag, "--target", self.args.target,
                "--title", self.args.title, "--body-file", str(self.notes), "--asset", str(self.file)]

    def remote_asset(self):
        return {"id": 10, "name": self.asset.name, "size": self.asset.size, "state": "uploaded",
                "digest": "sha256:" + self.asset.sha256}

    def remote_release(self, draft=True):
        return {"id": 7, "tag_name": self.args.tag, "name": self.args.title, "body": self.body,
                "target_commitish": self.args.target, "draft": draft, "prerelease": False}

    def client(self, draft=True):
        client = Mock()
        client.base = "/repos/owner/repository"
        client.request.side_effect = [{"full_name": self.args.repo}, {"sha": self.args.target}]
        client.tag_target.return_value = self.args.target
        client.release.return_value = self.remote_release(draft)
        client.assets.return_value = [self.remote_asset()]
        client.asset_hash.return_value = self.asset.sha256
        return client

    def test_default_dry_run_never_reads_credentials_or_network(self):
        output = io.StringIO()
        with patch.object(release, "credential_from_git") as credentials, patch.object(release, "GitHub") as network, redirect_stdout(output):
            self.assertEqual(release.main(self.cli()), 0)
        credentials.assert_not_called()
        network.assert_not_called()
        self.assertIn('"network": false', output.getvalue())

    def test_credentials_and_helper_errors_are_never_printed(self):
        secret = "PRIVATE_TOKEN_FIXTURE"
        output = io.StringIO()
        with patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=f"username=fixture\npassword={secret}\n", stderr=secret)) as helper, redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(release.credential_from_git(), secret)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(helper.call_args.kwargs["env"]["GCM_INTERACTIVE"], "never")
        self.assertTrue(helper.call_args.kwargs["capture_output"])
        with patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=secret, stderr=secret)):
            with self.assertRaises(release.PublishError) as error:
                release.credential_from_git()
        self.assertNotIn(secret, str(error.exception))

    def test_unsafe_endpoints_are_refused(self):
        for value in ("http://api.github.com/test", "https://api.github.com.evil.invalid/test",
                      "https://api.github.com@evil.invalid/test", "https://token@api.github.com/test",
                      "https://api.github.com:444/test", "https://api.github.com/test#fragment",
                      "https://api.github.com/test\r\nAuthorization: secret"):
            with self.subTest(url=value), self.assertRaises(release.PublishError):
                release.checked_url(value, release.DOWNLOAD_HOSTS)

    def test_download_redirect_never_forwards_authorization(self):
        client = release.GitHub(self.args.repo, "PRIVATE_TOKEN_FIXTURE")
        first = Mock(status=302)
        first.getheader.return_value = "https://release-assets.githubusercontent.com/fixture?signature=PRIVATE_SIGNATURE"
        second = Mock(status=200)
        second.read.side_effect = [b"fixture", b""]
        with patch.object(client, "_connect", side_effect=[(Mock(), first), (Mock(), second)]) as connect:
            self.assertEqual(client.asset_hash({"id": 1, "size": 7}), hashlib.sha256(b"fixture").hexdigest())
        self.assertTrue(connect.call_args_list[0].kwargs["auth"])
        self.assertFalse(connect.call_args_list[1].kwargs["auth"])

    def test_download_redirect_to_unknown_host_is_rejected(self):
        client = release.GitHub(self.args.repo, "PRIVATE_TOKEN_FIXTURE")
        response = Mock(status=302)
        response.getheader.return_value = "https://unapproved.invalid/secret"
        with patch.object(client, "_connect", return_value=(Mock(), response)) as connect:
            with self.assertRaises(release.PublishError):
                client.asset_hash({"id": 1, "size": 7})
        self.assertEqual(connect.call_count, 1)

    def test_existing_asset_conflict_is_not_replaced(self):
        client = Mock()
        client.asset_hash.return_value = "b" * 64
        with self.assertRaisesRegex(release.PublishError, "conflicts"):
            release.compare_assets(client, [self.remote_asset()], [self.asset])
        client.request.assert_not_called()

    def test_published_release_with_missing_asset_is_refused(self):
        client = self.client(draft=False)
        client.assets.return_value = []
        with self.assertRaisesRegex(release.PublishError, "Published release"):
            release.inspect_remote(client, self.args, [self.asset], self.body)
        self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list))

    def test_draft_metadata_or_remote_tag_conflicts_prevent_asset_writes(self):
        for field, value in (("name", "Different release"), ("body", "Different notes")):
            client = self.client()
            client.release.return_value[field] = value
            with self.subTest(field=field), self.assertRaises(release.PublishError):
                release.inspect_remote(client, self.args, [self.asset], self.body)
            client.assets.assert_not_called()
        client = self.client()
        client.tag_target.return_value = "b" * 40
        with self.assertRaisesRegex(release.PublishError, "another commit"):
            release.inspect_remote(client, self.args, [self.asset], self.body)
        client.release.assert_not_called()

    def test_matching_draft_is_resumable_and_matching_published_is_read_only(self):
        for draft in (True, False):
            client = self.client(draft)
            found, missing = release.inspect_remote(client, self.args, [self.asset], self.body)
            self.assertEqual(found["draft"], draft)
            self.assertEqual(missing, [])
            self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list))

    def test_publish_creates_draft_then_verifies_assets_before_publication(self):
        client = self.client()
        client.release.side_effect = [None, self.remote_release()]
        client.tag_target.return_value = None
        client._token = "PRIVATE_TOKEN_FIXTURE"
        def request(method, path, payload=None, **kwargs):
            if method == "GET" and path.endswith(self.args.target):
                return {"sha": self.args.target}
            if method == "GET":
                return {"full_name": self.args.repo}
            if method == "POST" and kwargs.get("upload"):
                return self.remote_asset()
            if method == "POST":
                self.assertTrue(payload["draft"])
                return self.remote_release()
            self.assertEqual(method, "PATCH")
            self.assertEqual(payload, {"draft": False, "make_latest": "true"})
            client.asset_hash.assert_called()
            return self.remote_release(draft=False)
        client.request.side_effect = request
        output = io.StringIO()
        with patch.object(release, "credential_from_git", return_value="PRIVATE_TOKEN_FIXTURE"), patch.object(release, "GitHub", return_value=client), redirect_stdout(output):
            self.assertEqual(release.main(self.cli() + ["--publish"]), 0)
        self.assertEqual([call.args[0] for call in client.request.call_args_list if call.args[0] != "GET"], ["POST", "POST", "PATCH"])
        self.assertNotIn("PRIVATE_TOKEN_FIXTURE", output.getvalue())
        self.assertEqual(client._token, "")


if __name__ == "__main__":
    unittest.main()
