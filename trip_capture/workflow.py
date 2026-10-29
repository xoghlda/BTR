"""Explicit browser workflows with memory-only credentials and evidence callbacks.

The engine reuses the selected-window guards from ``BrowserMacro``. Its adapter
and collector boundaries allow complete tests without Windows input or accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
from urllib.parse import urlsplit

from .browser_macro import (
    BROWSERS, BrowserMacro, Geometry, MacroError, MAX_STEP_WAIT_MS,
    MIN_STEP_WAIT_MS, POLL_SECONDS, _Stopped, _validate_snapshot,
)
from .native_capture import NativeBrowserWindow

MAX_WORKFLOW_BYTES = 128 * 1024
MAX_WORKFLOW_STEPS = 100
MAX_PDF_BYTES = 100 * 1024 * 1024
PDF_TIMEOUT_SECONDS = 60
NEW_WINDOW_TIMEOUT_SECONDS = 15
KINDS = frozenset(("open_url", "click", "text", "username", "password", "key", "pause", "switch_window", "capture", "pdf"))
POINT_KINDS = frozenset(("click", "text", "username", "password"))
KEYS = frozenset(("ENTER", "TAB", "SHIFT+TAB", "CTRL+A", "CTRL+P", "ESC", "SPACE", "DOWN", "UP", "HOME", "END"))
VARIABLES = frozenset(("trip_date", "trip_end_date", "trip_date_compact", "trip_end_date_compact", "origin", "destination", "rail_origin", "rail_destination"))
OFFICIAL_DOMAINS = {
    "naver": ("naver.com",), "opinet": ("opinet.co.kr",),
    "hipass": ("hipass.co.kr",), "korail": ("korail.com", "letskorail.com"),
}
_TOKEN = re.compile(r"\{([a-z_]+)\}")
_PDF_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MacroError("흐름의 위치 또는 대기 시간 값이 허용 범위를 벗어났습니다.")


def _string(value, maximum, *, empty=True):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise MacroError("흐름의 이름 또는 입력값 형식이 올바르지 않습니다.")


def _keys(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise MacroError("흐름 파일 형식이 올바르지 않거나 지원하지 않는 항목이 있습니다.")


def validate_official_url(site: str, value: str, *, allow_schemeless=False) -> str:
    """Return only an HTTPS URL on a site's exact official hostname boundary."""
    if site not in OFFICIAL_DOMAINS:
        raise MacroError("지원하는 사이트를 선택해 주세요.")
    _string(value, 8192, empty=False)
    if any(c.isspace() for c in value) or "\\" in value:
        raise MacroError("공식 사이트의 HTTPS 주소만 사용할 수 있습니다.")
    url = value
    if allow_schemeless and "://" not in url and not url.startswith(("/", "\\")):
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or not any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS[site])):
            raise ValueError
    except (ValueError, UnicodeError):
        raise MacroError("공식 사이트의 HTTPS 주소만 사용할 수 있습니다.") from None
    return url


def _check_template(value: str):
    if any(match.group(1) not in VARIABLES for match in _TOKEN.finditer(value)) or "{" in _TOKEN.sub("", value) or "}" in _TOKEN.sub("", value):
        raise MacroError("지원하지 않는 출장 변수 또는 잘못된 중괄호가 있습니다.")


def substitute_text(value: str, variables: dict[str, str]) -> str:
    _check_template(value)
    if any(match.group(1) not in variables for match in _TOKEN.finditer(value)):
        raise MacroError("입력 단계에 필요한 출장 변수값을 확인해 주세요.")
    return _TOKEN.sub(lambda match: variables[match.group(1)], value)


@dataclass(frozen=True)
class WorkflowStep:
    kind: str
    name: str
    site: str
    x: int = 0
    y: int = 0
    value: str = ""
    wait_ms: int = 1000

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in KINDS or not isinstance(self.site, str) or self.site not in OFFICIAL_DOMAINS:
            raise MacroError("지원하지 않는 단계 또는 사이트입니다.")
        _string(self.name, 80, empty=False)
        _string(self.value, 8192)
        _integer(self.x, -1, 32767)
        _integer(self.y, -1, 32767)
        _integer(self.wait_ms, MIN_STEP_WAIT_MS, MAX_STEP_WAIT_MS)
        if (self.x < 0) != (self.y < 0):
            raise MacroError("미등록 위치는 가로·세로 모두 -1이어야 합니다.")
        if self.kind in ("username", "password") and self.value:
            raise MacroError("아이디·비밀번호 단계의 값은 비워야 합니다. 계정은 실행할 때만 전달하세요.")
        if self.kind in ("click", "capture") and self.value:
            raise MacroError("클릭·화면 저장 단계의 값은 비워 주세요.")
        if self.kind == "open_url":
            validate_official_url(self.site, self.value)
        elif self.kind == "text":
            _check_template(self.value)
        elif self.kind == "key" and self.value not in KEYS:
            raise MacroError("허용된 키 입력만 사용할 수 있습니다.")
        elif self.kind == "pdf" and self.value and not _PDF_KEY.fullmatch(self.value):
            raise MacroError("PDF 단계에는 파일 경로 대신 등록한 경로의 키를 입력해 주세요.")

    def to_dict(self):
        return {"kind": self.kind, "name": self.name, "site": self.site,
                "x": self.x, "y": self.y, "value": self.value, "wait_ms": self.wait_ms}


@dataclass(frozen=True)
class WorkflowProfile:
    browser: str
    geometry: Geometry | None
    steps: tuple[WorkflowStep, ...]
    window_mode: str = "maximize"

    def __post_init__(self):
        if self.browser not in BROWSERS.values() or self.window_mode not in ("maximize", "recorded"):
            raise MacroError("흐름의 브라우저 또는 창 준비 방식이 올바르지 않습니다.")
        if self.geometry is not None and not isinstance(self.geometry, Geometry):
            raise MacroError("흐름의 창 정보가 올바르지 않습니다.")
        if type(self.steps) is not tuple or len(self.steps) > MAX_WORKFLOW_STEPS or not all(isinstance(s, WorkflowStep) for s in self.steps):
            raise MacroError("흐름은 최대 100개의 단계로 구성해 주세요.")
        if self.geometry is not None and any(s.kind in POINT_KINDS and s.x >= 0 and (s.x >= self.geometry.width or s.y >= self.geometry.height) for s in self.steps):
            raise MacroError("등록한 입력 위치가 기록한 창 영역을 벗어났습니다.")

    def to_dict(self):
        geometry = self.geometry
        return {"version": 1, "browser": self.browser, "window_mode": self.window_mode,
                "geometry": None if geometry is None else {"width": geometry.width, "height": geometry.height, "dpi": geometry.dpi},
                "steps": [step.to_dict() for step in self.steps]}

    @classmethod
    def from_dict(cls, value):
        _keys(value, {"version", "browser", "window_mode", "geometry", "steps"})
        if type(value["version"]) is not int or value["version"] != 1:
            raise MacroError("지원하지 않는 흐름 파일 버전입니다.")
        geometry = value["geometry"]
        if geometry is not None:
            _keys(geometry, {"width", "height", "dpi"})
            geometry = Geometry(**geometry)
        if type(value["steps"]) is not list or len(value["steps"]) > MAX_WORKFLOW_STEPS:
            raise MacroError("흐름의 단계 목록 형식이 올바르지 않습니다.")
        for step in value["steps"]:
            _keys(step, {"kind", "name", "site", "x", "y", "value", "wait_ms"})
        return cls(value["browser"], geometry, tuple(WorkflowStep(**step) for step in value["steps"]), value["window_mode"])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MacroError("중복된 항목이 있는 흐름 파일은 사용할 수 없습니다.")
        result[key] = value
    return result


def load_workflow(path: Path) -> WorkflowProfile:
    try:
        with Path(path).open("rb") as source:
            data = source.read(MAX_WORKFLOW_BYTES + 1)
        if len(data) > MAX_WORKFLOW_BYTES:
            raise MacroError("흐름 파일은 128 KiB 이하만 사용할 수 있습니다.")
        return WorkflowProfile.from_dict(json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object))
    except MacroError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise MacroError("흐름 JSON 파일을 읽지 못했습니다. 지원하는 파일인지 확인해 주세요.") from None


def save_workflow(path: Path, profile: WorkflowProfile) -> None:
    if not isinstance(profile, WorkflowProfile):
        raise MacroError("저장할 흐름 정보가 올바르지 않습니다.")
    data = (json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_WORKFLOW_BYTES:
        raise MacroError("흐름 파일은 128 KiB 이하로 저장해 주세요.")
    temporary = None
    try:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        os.replace(temporary, destination)
    except OSError:
        raise MacroError("흐름 파일을 저장하지 못했습니다. 폴더 권한을 확인해 주세요.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _pdf_fingerprint(path: Path):
    """Read only the explicitly supplied file, bounded in size; never scan a folder."""
    try:
        before = path.stat()
        if not path.is_file() or before.st_size > MAX_PDF_BYTES:
            raise MacroError("지정한 PDF 파일의 종류 또는 크기를 확인해 주세요.")
        digest = hashlib.sha256()
        header = b""
        total = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if not header:
                    header = chunk[:1024]
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise MacroError("지정한 PDF 파일의 크기를 확인해 주세요.")
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or total != after.st_size:
            return None
        return (after.st_size, after.st_mtime_ns, after.st_ino, digest.digest(), b"%PDF-" in header)
    except FileNotFoundError:
        return None
    except OSError:
        return None


class WorkflowEngine(BrowserMacro):
    def __init__(self, events: queue.Queue, adapter=None, collector=None):
        if adapter is None:
            from .workflow_native import WorkflowNativeAdapter
            adapter = WorkflowNativeAdapter()
        super().__init__(events, adapter)
        self.collector = collector
        self._resume_lock = threading.Lock()
        self._resume_signal = threading.Event()
        self._paused_job: str | None = None

    def resume(self, job_id: str) -> bool:
        with self._resume_lock:
            if self._paused_job != job_id:
                return False
            self._resume_signal.set()
            return True

    def cancel(self):
        super().cancel()
        self._resume_signal.set()

    def close(self):
        self._resume_signal.set()
        super().close()

    def _await_resume(self, job_id, stopped, message, emit):
        with self._resume_lock:
            self._resume_signal.clear()
            self._paused_job = job_id
        started = self.adapter.monotonic()
        emit("paused", message=message)
        try:
            while not self._resume_signal.wait(POLL_SECONDS):
                self._check_stop(stopped, float("inf"))
            self._check_stop(stopped, float("inf"))
        finally:
            with self._resume_lock:
                self._paused_job = None
        return self.adapter.monotonic() - started

    def _guard(self, identity, geometry, stopped, deadline):
        self._check_stop(stopped, deadline)
        _validate_snapshot(self.adapter.snapshot(identity), identity, geometry)
        self._check_stop(stopped, deadline)

    def _current_url(self, identity, site):
        try:
            return validate_official_url(site, self.adapter.current_url(identity), allow_schemeless=True)
        except Exception:
            return ""

    def _credential_url(self, identity, site):
        url = self._current_url(identity, site)
        # Naver has user-authored subdomains; only its dedicated login host may
        # receive account input, even though other official hosts may be opened.
        if site == "naver" and (not url or urlsplit(url).hostname != "nid.naver.com"):
            return ""
        return url

    def _new_window(self, current, browser, seen, title_filter, geometry, mode, stopped, deadline):
        until = min(deadline, self.adapter.monotonic() + NEW_WINDOW_TIMEOUT_SECONDS)
        while True:
            self._check_stop(stopped, deadline)
            candidates = []
            for window in self.adapter.list_windows():
                if window.hwnd in seen or window.browser != browser or (title_filter and title_filter not in window.title):
                    continue
                try:
                    identity = self.adapter.bind(window)
                    if os.path.normcase(identity.executable) != os.path.normcase(current.executable):
                        continue
                    candidates.append((window, identity))
                except Exception:
                    continue
            if len(candidates) > 1:
                raise MacroError("새 브라우저 창이 여러 개입니다. 출력 창을 하나만 열어 주세요.")
            if candidates:
                window, identity = candidates[0]
                prepared = self.adapter.prepare_window(identity, mode, geometry, stopped=stopped)
                _validate_snapshot(prepared, identity, geometry, require_foreground=False)
                seen.add(window.hwnd)
                return window, identity
            if self.adapter.monotonic() >= until - POLL_SECONDS:
                raise MacroError("새 브라우저 출력 창을 찾지 못했습니다.")
            self._pause(POLL_SECONDS, stopped, deadline)

    def _wait_pdf(self, path, baseline, stopped, deadline):
        until = min(deadline, self.adapter.monotonic() + PDF_TIMEOUT_SECONDS)
        previous, stable = None, 0
        while True:
            self._check_stop(stopped, deadline)
            current = _pdf_fingerprint(path)
            if current is not None and current != baseline and current[0] > 0 and current[-1]:
                stable = stable + 1 if current == previous else 1
                if stable >= 3:
                    return current
            else:
                stable = 0
            previous = current
            if self.adapter.monotonic() >= until:
                raise MacroError("이번 실행에서 새로 저장되거나 변경된 PDF를 찾지 못했습니다. 지정한 출력 파일 경로를 확인해 주세요.")
            self._pause(min(0.2, until - self.adapter.monotonic()), stopped, deadline)

    def play(self, window: NativeBrowserWindow, profile: WorkflowProfile, *, accounts=None, variables=None, pdf_paths=None) -> str:
        if not isinstance(profile, WorkflowProfile) or not profile.steps or profile.geometry is None:
            raise MacroError("실행할 흐름과 창 기준을 먼저 등록해 주세요.")
        if window.browser != profile.browser:
            raise MacroError("기록한 흐름과 같은 종류의 브라우저를 선택해 주세요.")
        if any(step.kind in POINT_KINDS and step.x < 0 for step in profile.steps):
            raise MacroError("미등록 입력 위치가 있습니다. 모든 입력 위치를 먼저 등록해 주세요.")
        try:
            account_values = dict(accounts or {})
            if any(site not in OFFICIAL_DOMAINS or not isinstance(pair, (tuple, list)) or len(pair) != 2
                   or any(not isinstance(value, str) or len(value) > 4096 or "\x00" in value for value in pair)
                   for site, pair in account_values.items()):
                raise ValueError
            account_values = {site: tuple(pair) for site, pair in account_values.items()}
            runtime_variables = dict(variables or {})
            if any(key not in VARIABLES or not isinstance(value, str) or len(value) > 8192 for key, value in runtime_variables.items()):
                raise ValueError
            runtime_variables.setdefault("trip_end_date", runtime_variables.get("trip_date", ""))
            for key, source in (("trip_date_compact", "trip_date"), ("trip_end_date_compact", "trip_end_date")):
                if source in runtime_variables:
                    runtime_variables.setdefault(key, runtime_variables[source].replace("-", ""))
            texts = {index: substitute_text(step.value, runtime_variables) for index, step in enumerate(profile.steps) if step.kind == "text"}
            paths = dict(pdf_paths or {})
            used_keys = {step.value or "receipt_pdf" for step in profile.steps if step.kind == "pdf"}
            path_values = {}
            for key in used_keys:
                if key not in paths or not isinstance(paths[key], (str, os.PathLike)):
                    raise ValueError
                path = Path(paths[key]).expanduser().absolute()
                if path.suffix.lower() != ".pdf" or path.is_dir():
                    raise ValueError
                path_values[key] = path
            if any(step.kind in ("capture", "pdf") for step in profile.steps) and self.collector is None:
                raise ValueError
        except Exception:
            raise MacroError("계정·출장 변수·PDF 파일 경로 또는 증빙 저장 설정을 확인해 주세요.") from None

        def playback(job_id, stopped):
            count, status, message = 0, "complete", ""
            report = None
            secrets = sorted({value for pair in account_values.values() for value in pair if value}, key=len, reverse=True)
            def emit(kind, **values):
                for key in ("name", "message"):
                    if key in values:
                        for secret in secrets:
                            values[key] = values[key].replace(secret, "[비공개]")
                self._emit(job_id, kind, **values)
            try:
                baseline = {key: _pdf_fingerprint(path) for key, path in path_values.items()}
                if any(baseline[key] is None and path.exists() for key, path in path_values.items()):
                    raise MacroError("실행 전 PDF 파일 상태를 확인하지 못했습니다.")
                seen = {candidate.hwnd for candidate in self.adapter.list_windows()}
                seen.add(window.hwnd)
                active_window = window
                identity = self.adapter.bind(active_window)
                deadline = self.adapter.monotonic() + 120 + sum(step.wait_ms / 1000 + (PDF_TIMEOUT_SECONDS if step.kind == "pdf" else NEW_WINDOW_TIMEOUT_SECONDS if step.kind == "switch_window" else 0) for step in profile.steps)
                emit("preparing", window_mode=profile.window_mode)
                prepared = self.adapter.prepare_window(identity, profile.window_mode, profile.geometry, stopped=stopped)
                _validate_snapshot(prepared, identity, profile.geometry, require_foreground=False)
                self._countdown(job_id, "playing", stopped, deadline)
                for index, step in enumerate(profile.steps):
                    self._check_stop(stopped, deadline)
                    if step.kind == "pause":
                        deadline += self._await_resume(job_id, stopped, step.value or "브라우저에서 인증 또는 확인을 완료한 뒤 계속을 눌러 주세요.", emit)
                        self._countdown(job_id, "playing", stopped, deadline)
                        self._guard(identity, profile.geometry, stopped, deadline)
                    elif step.kind == "switch_window":
                        emit("preparing", window_mode=profile.window_mode)
                        active_window, identity = self._new_window(identity, profile.browser, seen, step.value, profile.geometry, profile.window_mode, stopped, deadline)
                        self._countdown(job_id, "playing", stopped, deadline)
                        self._guard(identity, profile.geometry, stopped, deadline)
                    elif step.kind == "pdf":
                        key = step.value or "receipt_pdf"
                        fingerprint = self._wait_pdf(path_values[key], baseline[key], stopped, deadline)
                        self._check_stop(stopped, deadline)
                        self.collector.import_pdf(path_values[key], step.site, self._current_url(identity, step.site))
                        baseline[key] = fingerprint
                    else:
                        self._guard(identity, profile.geometry, stopped, deadline)
                        if step.kind == "open_url":
                            self.adapter.navigate(identity, profile.geometry, step.value, stopped=stopped)
                        elif step.kind == "click":
                            self.adapter.left_click(identity, profile.geometry, step.x, step.y)
                        elif step.kind in ("text", "username", "password"):
                            value = texts[index] if step.kind == "text" else account_values.get(step.site, ("", ""))[step.kind == "password"]
                            verified = step.kind == "text" or bool(value and self._credential_url(identity, step.site))
                            if verified:
                                self.adapter.left_click(identity, profile.geometry, step.x, step.y)
                                self._guard(identity, profile.geometry, stopped, deadline)
                                verified = step.kind == "text" or bool(self._credential_url(identity, step.site))
                            if not verified:
                                deadline += self._await_resume(job_id, stopped, "공식 로그인 주소를 확인하지 못했거나 계정이 없습니다. 브라우저에서 이 입력을 직접 완료한 뒤 계속을 누르세요. 이 단계는 자동 입력하지 않습니다.", emit)
                                self._countdown(job_id, "playing", stopped, deadline)
                                self._guard(identity, profile.geometry, stopped, deadline)
                            else:
                                try:
                                    self.adapter.type_text(identity, profile.geometry, value, clear=True, stopped=stopped)
                                except Exception:
                                    self._check_stop(stopped, deadline)
                                    if step.kind == "text":
                                        raise
                                    deadline += self._await_resume(job_id, stopped, "이 로그인 칸의 자동 입력을 완료하지 못했습니다. 브라우저에서 이 칸을 직접 다시 입력한 뒤 계속을 누르세요. 자동 입력은 재시도하지 않습니다.", emit)
                                    self._countdown(job_id, "playing", stopped, deadline)
                                    self._guard(identity, profile.geometry, stopped, deadline)
                            value = ""
                        elif step.kind == "key":
                            self.adapter.press_key(identity, profile.geometry, step.value, stopped=stopped)
                        elif step.kind == "capture":
                            self.collector.capture(active_window, step.site, step.name, self._current_url(identity, step.site))
                    self._check_stop(stopped, deadline)
                    count += 1
                    emit("progress", index=index + 1, total=len(profile.steps), name=step.name)
                    self._pause(step.wait_ms / 1000, stopped, deadline)
            except _Stopped as error:
                status, message = "cancelled", str(error)
            except Exception:
                status, message = "failed", "전체 흐름을 진행하지 못했습니다. 입력 위치·창 상태·조회 결과와 출력 경로를 확인해 주세요. 저장된 자료는 보고서에 보존합니다."
            finally:
                with self._resume_lock:
                    self._paused_job = None
                if self.collector is not None:
                    try:
                        report = self.collector.finish(status)
                    except Exception:
                        status, message = "failed", "보고서를 저장하지 못했습니다. 출력 폴더와 저장된 원본 자료를 확인해 주세요."
                account_values.clear()
                runtime_variables.clear()
                texts.clear()
                if status == "complete":
                    emit("done", count=count, report=str(report) if report is not None else None)
                else:
                    emit("stopped" if status == "cancelled" else "error", message=message, count=count, report=str(report) if report is not None else None)
                secrets.clear()
        return self._start(playback)
