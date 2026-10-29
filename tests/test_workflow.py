"""Workflow regression tests: fake browser, fake clock, no Windows input."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest

from trip_capture.browser_macro import Geometry, MacroError, MacroProfile, WindowIdentity, WindowSnapshot
from trip_capture.native_capture import NativeBrowserWindow
from trip_capture.workflow import (
    MAX_WORKFLOW_BYTES, WorkflowEngine, WorkflowProfile, WorkflowStep,
    load_workflow, save_workflow, substitute_text, validate_official_url,
)


class FakeAdapter:
    def __init__(self):
        self.window = NativeBrowserWindow(101, "Development browser", "Chrome")
        self.identity = WindowIdentity(101, 202, 303, "C:/fake/chrome.exe")
        self.geometry = Geometry(800, 600, 96)
        self.identities = {101: self.identity}
        self.windows = [self.window]
        self.states = {101: WindowSnapshot(self.identity, self.geometry, 100, 100)}
        self.url = "https://www.korail.com/ticket/login"
        self.cursor = (150, 180)
        self.now = 0.0
        self.escape = False
        self.actions = []
        self.on_wait = None
        self.on_click = None
        self.on_navigate = None
        self.on_type = None

    def list_windows(self):
        return list(self.windows)

    def bind(self, window):
        return self.identities[window.hwnd]

    def snapshot(self, identity):
        return self.states[identity.hwnd]

    def prepare_window(self, identity, mode, expected_geometry=None, *, stopped=None):
        self.actions.append(("prepare", identity.hwnd, mode, expected_geometry))
        return self.snapshot(identity)

    def cursor_position(self):
        return self.cursor

    def escape_pressed(self):
        return self.escape

    def left_click(self, identity, geometry, x, y):
        self.actions.append(("click", identity.hwnd, x, y))
        if self.on_click:
            self.on_click(self)

    def type_text(self, identity, geometry, text, *, clear=True, stopped=None):
        self.actions.append(("text", identity.hwnd, text, clear))
        if self.on_type:
            self.on_type(self)

    def press_key(self, identity, geometry, key, *, stopped=None):
        self.actions.append(("key", identity.hwnd, key))

    def navigate(self, identity, geometry, url, *, stopped=None):
        self.actions.append(("url", identity.hwnd, url))
        self.url = url
        if self.on_navigate:
            self.on_navigate(self)

    def current_url(self, identity):
        return self.url

    def monotonic(self):
        return self.now

    def wait(self, seconds, stopped):
        self.now += seconds
        if self.on_wait:
            self.on_wait(self, stopped)

    def popup(self, hwnd=102, title="Receipt", executable=None, geometry=None):
        window = NativeBrowserWindow(hwnd, title, "Chrome")
        identity = WindowIdentity(hwnd, hwnd + 100, hwnd + 200, executable or self.identity.executable)
        self.windows.append(window)
        self.identities[hwnd] = identity
        self.states[hwnd] = WindowSnapshot(identity, geometry or self.geometry, 200, 200)
        return window


class Collector:
    def __init__(self):
        self.calls = []

    def capture(self, window, site, name, url):
        self.calls.append(("capture", window.hwnd, site, name, url))

    def import_pdf(self, path, site, url):
        self.calls.append(("pdf", Path(path), site, url))

    def finish(self, status):
        self.calls.append(("finish", status))
        return Path("C:/fake/report.html")


class WorkflowValidationTests(unittest.TestCase):
    def profile(self, *steps, geometry=Geometry(800, 600, 96)):
        return WorkflowProfile("Chrome", geometry, tuple(steps))

    def test_profile_roundtrip_keeps_unregistered_draft_and_no_accounts(self):
        profile = self.profile(WorkflowStep("username", "아이디", "korail", -1, -1),
                               WorkflowStep("password", "비밀번호", "korail", -1, -1), geometry=None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            save_workflow(path, profile)
            self.assertEqual(load_workflow(path), profile)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 1)
        self.assertIsNone(saved["geometry"])
        self.assertEqual([step["value"] for step in saved["steps"]], ["", ""])
        self.assertNotIn("accounts", saved)

    def test_credentials_cannot_be_serialized_in_value_or_unknown_keys(self):
        for kind in ("username", "password"):
            with self.subTest(kind=kind), self.assertRaises(MacroError):
                WorkflowStep(kind, "입력", "hipass", value="must-not-store")
        value = self.profile(WorkflowStep("click", "확인", "hipass")).to_dict()
        value["accounts"] = {"hipass": ["user", "secret"]}
        with self.assertRaises(MacroError):
            WorkflowProfile.from_dict(value)

    def test_strict_json_size_duplicate_boolean_and_step_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            for data in (b"{" + b" " * MAX_WORKFLOW_BYTES + b"}", b'{"version":1,"version":1}'):
                path.write_bytes(data)
                with self.assertRaises(MacroError):
                    load_workflow(path)
        for kwargs in ({"x": True}, {"wait_ms": 199}, {"wait_ms": 30001}, {"x": -1, "y": 0}):
            with self.assertRaises(MacroError):
                WorkflowStep("click", "확인", "naver", **kwargs)
        with self.assertRaises(MacroError):
            self.profile(*(WorkflowStep("click", "확인", "naver") for _ in range(101)))
        data = self.profile(WorkflowStep("click", "확인", "naver")).to_dict()
        data["version"] = True
        with self.assertRaises(MacroError):
            WorkflowProfile.from_dict(data)

    def test_official_url_hostname_and_scheme_boundary(self):
        for site, url in (("naver", "https://map.naver.com/"), ("korail", "https://www.letskorail.com/"),
                          ("hipass", "https://hipass.co.kr:443/path")):
            self.assertEqual(validate_official_url(site, url), url)
        self.assertEqual(validate_official_url("korail", "www.korail.com/ticket", allow_schemeless=True), "https://www.korail.com/ticket")
        for url in ("http://www.korail.com/", "https://korail.com.evil.test/", "https://evilkorail.com/",
                    "https://user:password@korail.com/", "https://korail.com:444/", "//korail.com/",
                    "https://korail.com\\@evil.test/", "https://korail.com./", "file:///korail.com"):
            with self.subTest(url=url), self.assertRaises(MacroError):
                validate_official_url("korail", url, allow_schemeless=True)

    def test_text_substitution_allows_only_eight_exact_variables(self):
        values = {"trip_date": "2026-10-20", "trip_end_date_compact": "20261021", "origin": "오송"}
        self.assertEqual(substitute_text("{origin}/{trip_date}/{trip_end_date_compact}", values), "오송/2026-10-20/20261021")
        for template in ("{unknown}", "{origin.__class__}", "{origin[0]}", "{origin!r}", "{origin:>20}", "{{origin}}", "{origin"):
            with self.subTest(template=template), self.assertRaises(MacroError):
                WorkflowStep("text", "날짜", "korail", value=template)
        with self.assertRaises(MacroError):
            substitute_text("{destination}", values)

    def test_keys_are_allowlisted(self):
        for key in ("ENTER", "DOWN", "UP", "HOME", "END", "CTRL+P", "SHIFT+TAB"):
            WorkflowStep("key", "키", "korail", value=key)
        with self.assertRaises(MacroError):
            WorkflowStep("key", "위험 키", "korail", value="ALT+F4")


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.adapter = FakeAdapter()
        self.collector = Collector()
        self.engine = WorkflowEngine(self.events, self.adapter, self.collector)
        self.addCleanup(self.engine.close)

    def profile(self, *steps):
        return WorkflowProfile("Chrome", self.adapter.geometry, tuple(steps))

    def completed(self, job_id, *, on_event=None):
        result = []
        while True:
            event = self.events.get(timeout=3)
            self.assertEqual(event["job_id"], job_id)
            result.append(event)
            if on_event:
                on_event(event)
            if event["type"] == "idle":
                return result

    def terminal(self, events):
        return next(event for event in events if event["type"] in ("done", "error", "stopped"))

    def test_navigation_inputs_keys_capture_and_finish(self):
        profile = self.profile(
            WorkflowStep("open_url", "로그인", "korail", value="https://www.korail.com/ticket/login", wait_ms=200),
            WorkflowStep("username", "아이디", "korail", 10, 20, wait_ms=200),
            WorkflowStep("password", "암호", "korail", 30, 40, wait_ms=200),
            WorkflowStep("text", "날짜", "korail", 50, 60, "{trip_date_compact}/{trip_end_date_compact}", 200),
            WorkflowStep("key", "조회", "korail", value="ENTER", wait_ms=200),
            WorkflowStep("capture", "영수증", "korail", wait_ms=200))
        job = self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-user", "fixture-secret")},
                               variables={"trip_date": "2026-10-20", "trip_end_date": "2026-10-21"})
        events = self.completed(job)
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertEqual(self.terminal(events)["count"], 6)
        self.assertEqual(self.terminal(events)["report"], "C:\\fake\\report.html")
        self.assertEqual([a[2] for a in self.adapter.actions if a[0] == "text"], ["fixture-user", "fixture-secret", "20261020/20261021"])
        self.assertTrue(all(a[3] for a in self.adapter.actions if a[0] == "text"))
        self.assertEqual([a[0] for a in self.adapter.actions], ["prepare", "url", "click", "text", "click", "text", "click", "text", "key"])
        self.assertEqual(self.collector.calls[-1], ("finish", "complete"))
        self.assertEqual(self.collector.calls[0][0], "capture")
        self.assertNotIn("fixture-secret", str(events))
        self.assertNotIn("fixture-user", str(events))

    def test_editing_only_profile_unregistered_and_missing_pdf_rejected_before_job(self):
        for profile, kwargs in ((WorkflowProfile("Chrome", None, (WorkflowStep("click", "입력", "korail"),)), {}),
                                (self.profile(WorkflowStep("click", "미등록", "korail", -1, -1)), {}),
                                (self.profile(WorkflowStep("pdf", "PDF", "korail")), {})):
            with self.subTest(profile=profile), self.assertRaises(MacroError):
                self.engine.play(self.adapter.window, profile, **kwargs)
        self.assertFalse(self.engine.is_busy())
        self.assertTrue(self.events.empty())
        self.assertEqual(self.adapter.actions, [])

    def test_zero_zero_coordinate_is_valid(self):
        events = self.completed(self.engine.play(self.adapter.window, self.profile(WorkflowStep("click", "모서리", "naver", 0, 0, wait_ms=200))))
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertIn(("click", 101, 0, 0), self.adapter.actions)

    def test_pause_requires_matching_resume_then_four_second_countdown(self):
        profile = self.profile(WorkflowStep("pause", "인증", "korail", value="직접 인증하세요.", wait_ms=200),
                               WorkflowStep("click", "완료", "korail", 20, 30, wait_ms=200))
        received = []
        def on_event(event):
            if event["type"] == "paused":
                self.assertFalse(self.engine.resume("stale-job"))
                self.assertTrue(self.engine.is_busy())
                self.assertFalse(any(a[0] == "click" for a in self.adapter.actions))
                received.append(event)
                self.assertTrue(self.engine.resume(event["job_id"]))
        events = self.completed(self.engine.play(self.adapter.window, profile), on_event=on_event)
        self.assertEqual(len(received), 1)
        self.assertEqual([e["remaining"] for e in events if e["type"] == "countdown"], [4, 3, 2, 1] * 2)
        self.assertEqual(self.terminal(events)["count"], 2)
        self.assertFalse(self.engine.resume(events[-1]["job_id"]))

    def test_cancel_while_paused_finishes_partial_report_once(self):
        profile = self.profile(WorkflowStep("capture", "첫 원본", "korail", wait_ms=200), WorkflowStep("pause", "인증", "korail"), WorkflowStep("click", "이후", "korail", 20, 30))
        events = self.completed(self.engine.play(self.adapter.window, profile), on_event=lambda event: self.engine.cancel() if event["type"] == "paused" else None)
        self.assertEqual(self.terminal(events)["type"], "stopped")
        self.assertEqual(self.terminal(events)["count"], 1)
        self.assertTrue(self.terminal(events)["report"])
        self.assertEqual([c for c in self.collector.calls if c[0] == "finish"], [("finish", "cancelled")])
        self.assertFalse(any(a[0] == "click" for a in self.adapter.actions))

    def test_unverified_credential_is_manual_once_with_no_automatic_retry(self):
        self.adapter.url = "https://korail.com.evil.test/login"
        profile = self.profile(WorkflowStep("password", "암호", "korail", 20, 30, wait_ms=200), WorkflowStep("click", "이후", "korail", 50, 60, wait_ms=200))
        def resume(event):
            if event["type"] == "paused":
                self.adapter.url = "https://www.korail.com/ticket/login"
                self.engine.resume(event["job_id"])
        events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-id", "fixture-password")}), on_event=resume)
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertEqual(sum(e["type"] == "paused" for e in events), 1)
        self.assertFalse(any(a[0] == "text" for a in self.adapter.actions))
        self.assertEqual([a for a in self.adapter.actions if a[0] == "click"], [("click", 101, 50, 60)])
        self.assertNotIn("fixture-password", str(events))

    def test_url_rechecked_after_field_click_before_credentials(self):
        self.adapter.on_click = lambda adapter: setattr(adapter, "url", "https://evil.test/")
        events = self.completed(self.engine.play(self.adapter.window, self.profile(WorkflowStep("username", "아이디", "korail", 20, 30, wait_ms=200)), accounts={"korail": ("fixture-id", "fixture-secret")}),
                                on_event=lambda event: self.engine.resume(event["job_id"]) if event["type"] == "paused" else None)
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertFalse(any(a[0] == "text" for a in self.adapter.actions))
        self.assertEqual(sum(e["type"] == "paused" for e in events), 1)

    def test_raw_exception_and_progress_names_do_not_leak_accounts(self):
        def fail(adapter):
            raise RuntimeError("fixture-id / fixture-secret")
        self.adapter.on_type = fail
        profile = self.profile(WorkflowStep("text", "fixture-id", "korail", 20, 30, value="ordinary text", wait_ms=200))
        events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-id", "fixture-secret")}))
        self.assertEqual(self.terminal(events)["type"], "error")
        self.assertEqual(self.collector.calls[-1], ("finish", "failed"))
        self.assertNotIn("fixture-secret", str(events))
        self.assertNotIn("fixture-id", str(events))
        self.adapter.on_type = None
        events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-id", "fixture-secret")}))
        self.assertNotIn("fixture-id", str(events))

    def test_failed_secure_input_pauses_for_manual_entry_and_does_not_retry(self):
        def fail(adapter):
            raise RuntimeError("fixture-id / fixture-secret")
        self.adapter.on_type = fail
        profile = self.profile(WorkflowStep("password", "암호", "korail", 20, 30, wait_ms=200), WorkflowStep("click", "다음", "korail", 50, 60, wait_ms=200))
        events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-id", "fixture-secret")}),
                                on_event=lambda event: self.engine.resume(event["job_id"]) if event["type"] == "paused" else None)
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertEqual(sum(e["type"] == "paused" for e in events), 1)
        self.assertEqual(sum(a[0] == "text" for a in self.adapter.actions), 1)
        self.assertIn(("click", 101, 50, 60), self.adapter.actions)
        self.assertNotIn("fixture-secret", str(events))
        self.assertNotIn("fixture-id", str(events))

    def test_cancel_during_failed_secure_input_does_not_offer_manual_retry(self):
        def fail(adapter):
            self.engine.cancel()
            raise RuntimeError("fixture-secret")
        self.adapter.on_type = fail
        profile = self.profile(WorkflowStep("password", "암호", "korail", 20, 30, wait_ms=200))
        events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"korail": ("fixture-id", "fixture-secret")}))
        self.assertEqual(self.terminal(events)["type"], "stopped")
        self.assertFalse(any(e["type"] == "paused" for e in events))
        self.assertEqual(self.collector.calls[-1], ("finish", "cancelled"))

    def test_naver_credentials_only_automate_on_dedicated_login_host(self):
        for url, allowed in (("https://blog.naver.com/fixture", False), ("https://naver.com/", False),
                             ("https://nid.naver.com/nidlogin.login", True)):
            with self.subTest(url=url):
                self.adapter.url = url
                self.adapter.actions.clear()
                profile = self.profile(WorkflowStep("username", "아이디", "naver", 20, 30, wait_ms=200))
                events = self.completed(self.engine.play(self.adapter.window, profile, accounts={"naver": ("fixture-id", "fixture-secret")}),
                                        on_event=lambda event: self.engine.resume(event["job_id"]) if event["type"] == "paused" else None)
                self.assertEqual(self.terminal(events)["type"], "done")
                self.assertEqual(any(a[0] == "text" for a in self.adapter.actions), allowed)
                self.assertEqual(any(e["type"] == "paused" for e in events), not allowed)

    def test_focus_identity_and_geometry_changes_stop_before_next_click(self):
        for change in (lambda state: replace(state, foreground=False), lambda state: replace(state, geometry=Geometry(900, 600, 96)), lambda state: replace(state, identity=replace(state.identity, created=999))):
            with self.subTest(change=change):
                self.adapter.actions.clear()
                self.adapter.states[101] = WindowSnapshot(self.adapter.identity, self.adapter.geometry, 100, 100)
                self.adapter.on_click = lambda adapter: adapter.states.__setitem__(101, change(adapter.states[101]))
                profile = self.profile(WorkflowStep("click", "첫 클릭", "korail", 20, 30, wait_ms=200), WorkflowStep("click", "다음", "korail", 40, 50, wait_ms=200))
                events = self.completed(self.engine.play(self.adapter.window, profile))
                self.assertEqual(self.terminal(events)["type"], "error")
                self.assertEqual(sum(a[0] == "click" for a in self.adapter.actions), 1)

    def test_switch_selects_only_new_matching_executable_window(self):
        self.adapter.popup(105, "Existing receipt")
        def popup(adapter):
            adapter.popup(102, "Receipt new")
            adapter.popup(103, "Receipt foreign", "C:/foreign/chrome.exe")
        self.adapter.on_navigate = popup
        profile = self.profile(WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200),
                               WorkflowStep("switch_window", "새 영수증", "korail", value="Receipt", wait_ms=200),
                               WorkflowStep("capture", "새 원본", "korail", wait_ms=200))
        events = self.completed(self.engine.play(self.adapter.window, profile))
        self.assertEqual(self.terminal(events)["type"], "done")
        self.assertEqual(self.collector.calls[0][0:2], ("capture", 102))
        self.assertEqual([a[1:3] for a in self.adapter.actions if a[0] == "prepare"], [(101, "maximize"), (102, "maximize")])

    def test_switch_preserves_selected_mode_and_fits_recorded_popup_geometry(self):
        original_prepare = self.adapter.prepare_window
        def prepare(identity, mode, expected_geometry=None, *, stopped=None):
            # A real maximized popup cannot retain a smaller recorded client.
            if identity.hwnd != 101:
                actual = Geometry(1600, 900, 96) if mode == "maximize" else expected_geometry
                self.adapter.states[identity.hwnd] = replace(self.adapter.states[identity.hwnd], geometry=actual)
            return original_prepare(identity, mode, expected_geometry, stopped=stopped)
        self.adapter.prepare_window = prepare
        for mode, geometry in (("recorded", Geometry(800, 600, 96)), ("maximize", Geometry(1600, 900, 96))):
            with self.subTest(mode=mode):
                self.adapter.actions.clear()
                self.collector.calls.clear()
                self.adapter.windows = [self.adapter.window]
                self.adapter.geometry = geometry
                self.adapter.states[101] = replace(self.adapter.states[101], geometry=geometry)
                self.adapter.on_navigate = lambda adapter: adapter.popup(102, "Receipt")
                profile = WorkflowProfile("Chrome", geometry, (
                    WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200),
                    WorkflowStep("switch_window", "새 영수증", "korail", value="Receipt", wait_ms=200),
                    WorkflowStep("capture", "원본", "korail", wait_ms=200)), mode)
                events = self.completed(self.engine.play(self.adapter.window, profile))
                self.assertEqual(self.terminal(events)["type"], "done")
                self.assertEqual([e["window_mode"] for e in events if e["type"] == "preparing"], [mode, mode])
                self.assertEqual([a[1:3] for a in self.adapter.actions if a[0] == "prepare"], [(101, mode), (102, mode)])
                self.assertEqual(self.collector.calls[0][0:2], ("capture", 102))

    def test_ambiguous_new_windows_and_geometry_mismatch_fail_without_capture(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.adapter.windows = [self.adapter.window]
                self.collector.calls.clear()
                def create(adapter):
                    for index in range(count):
                        adapter.popup(102 + index, "Receipt", geometry=Geometry(900, 600, 96) if count == 1 else None)
                self.adapter.on_navigate = create
                profile = self.profile(WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200), WorkflowStep("switch_window", "전환", "korail", wait_ms=200), WorkflowStep("capture", "원본", "korail", wait_ms=200))
                events = self.completed(self.engine.play(self.adapter.window, profile))
                self.assertEqual(self.terminal(events)["type"], "error")
                self.assertEqual(self.collector.calls, [("finish", "failed")])

    def test_existing_window_is_never_reused_as_popup(self):
        self.adapter.popup(105, "Receipt existing")
        events = self.completed(self.engine.play(self.adapter.window, self.profile(WorkflowStep("switch_window", "새 창", "korail", value="Receipt", wait_ms=200))))
        self.assertNotEqual(self.terminal(events)["type"], "done")
        self.assertEqual(sum(a[0] == "prepare" for a in self.adapter.actions), 1)

    def test_fresh_pdf_new_file_imports_exact_path_after_stability(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.pdf"
            unrelated = Path(directory) / "other.pdf"
            unrelated.write_bytes(b"%PDF-1.4\nold unrelated")
            data = b"%PDF-1.4\nfixture fresh\n%%EOF\n"
            def save(adapter):
                path.write_bytes(data)
            self.adapter.on_navigate = save
            profile = self.profile(WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200), WorkflowStep("pdf", "원본 PDF", "korail", wait_ms=200))
            events = self.completed(self.engine.play(self.adapter.window, profile, pdf_paths={"receipt_pdf": path}))
            self.assertEqual(self.terminal(events)["type"], "done")
            self.assertEqual(self.collector.calls[0][0:3], ("pdf", path, "korail"))
            self.assertEqual(path.read_bytes(), data)
            self.assertGreaterEqual(self.adapter.now, 4.8)

    def test_unchanged_existing_pdf_and_invalid_new_pdf_are_not_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.pdf"
            for old, new in ((b"%PDF-1.4\nold", None), (None, b"not a PDF")):
                with self.subTest(old=old, new=new):
                    self.collector.calls.clear()
                    path.unlink(missing_ok=True)
                    if old:
                        path.write_bytes(old)
                    self.adapter.on_navigate = lambda adapter: path.write_bytes(new) if new else None
                    profile = self.profile(WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200), WorkflowStep("pdf", "PDF", "korail", wait_ms=200))
                    events = self.completed(self.engine.play(self.adapter.window, profile, pdf_paths={"receipt_pdf": path}))
                    self.assertEqual(self.terminal(events)["type"], "error")
                    self.assertEqual(self.collector.calls, [("finish", "failed")])

    def test_changed_existing_pdf_imports_once_but_unchanged_second_stage_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.pdf"
            path.write_bytes(b"%PDF-1.4\nold")
            self.adapter.on_navigate = lambda adapter: path.write_bytes(b"%PDF-1.4\nnew fixture data")
            profile = self.profile(WorkflowStep("open_url", "출력", "korail", value="https://www.korail.com/ticket", wait_ms=200), WorkflowStep("pdf", "첫 PDF", "korail", wait_ms=200), WorkflowStep("pdf", "중복 금지", "korail", wait_ms=200))
            events = self.completed(self.engine.play(self.adapter.window, profile, pdf_paths={"receipt_pdf": path}))
            self.assertEqual(self.terminal(events)["type"], "error")
            self.assertEqual(sum(c[0] == "pdf" for c in self.collector.calls), 1)
            self.assertEqual(self.collector.calls[-1], ("finish", "failed"))

    def test_cancel_during_pdf_wait_and_escape_before_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.pdf"
            self.adapter.on_wait = lambda adapter, stopped: stopped.set() if adapter.now > 5 else None
            events = self.completed(self.engine.play(self.adapter.window, self.profile(WorkflowStep("pdf", "PDF", "korail", wait_ms=200)), pdf_paths={"receipt_pdf": path}))
        self.assertEqual(self.terminal(events)["type"], "stopped")
        self.assertEqual(self.collector.calls, [("finish", "cancelled")])
        self.adapter.escape = True
        events = self.completed(self.engine.play(self.adapter.window, self.profile(WorkflowStep("click", "클릭", "korail", 20, 30))))
        self.assertEqual(self.terminal(events)["type"], "stopped")
        self.assertFalse(any(a[0] == "click" for a in self.adapter.actions))

    def test_record_point_retains_browser_macro_api_and_event_objects(self):
        basis = MacroProfile("Chrome", self.adapter.geometry, ())
        events = self.completed(self.engine.record_point(self.adapter.window, "등록", 200, basis))
        recorded = next(e for e in events if e["type"] == "recorded")
        self.assertEqual((recorded["step"].x, recorded["step"].y), (50, 80))
        self.assertEqual(recorded["geometry"], self.adapter.geometry)
        self.assertEqual(recorded["window_mode"], "maximize")
        self.assertEqual(self.collector.calls, [])


if __name__ == "__main__":
    unittest.main()
