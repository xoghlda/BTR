"""The macro engine is tested with fake adapters; no Windows input is sent."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from trip_capture.browser_macro import (
    BrowserMacro, ClickStep, Geometry, MacroError, MacroProfile,
    MAX_PROFILE_BYTES, MAX_RUNTIME_SECONDS, Win32Adapter, WindowIdentity,
    WindowSnapshot, load_profile, save_profile,
)
from trip_capture.native_capture import NativeBrowserWindow


class FakeAdapter:
    def __init__(self):
        self.identity = WindowIdentity(101, 202, 303, "chrome.exe")
        self.window = NativeBrowserWindow(101, "Development browser", "Chrome")
        self.geometry = Geometry(800, 600, 96)
        self.state = WindowSnapshot(self.identity, self.geometry, 100, 100)
        self.cursor = (150, 180)
        self.escape = False
        self.now = 0.0
        self.clicks = []
        self.on_wait = None
        self.on_click = None

    def list_windows(self):
        return [self.window]

    def bind(self, window):
        return self.identity

    def snapshot(self, identity):
        return self.state

    def cursor_position(self):
        return self.cursor

    def escape_pressed(self):
        return self.escape

    def left_click(self, identity, geometry, x, y):
        self.clicks.append((self.state.left + x, self.state.top + y))
        self.cursor = self.clicks[-1]
        if self.on_click:
            self.on_click(self)

    def monotonic(self):
        return self.now

    def wait(self, seconds, stopped):
        self.now += seconds
        if self.on_wait:
            self.on_wait(self, stopped)


class BrowserMacroTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.adapter = FakeAdapter()
        self.macro = BrowserMacro(self.events, self.adapter)
        self.addCleanup(self.macro.close)
        self.profile = MacroProfile("Chrome", self.adapter.geometry, (
            ClickStep("첫 번째", 20, 40, 250), ClickStep("두 번째", 70, 90, 200),
        ))

    def completed_events(self, job_id):
        result = []
        while True:
            event = self.events.get(timeout=2)
            self.assertEqual(event["job_id"], job_id)
            result.append(event)
            if event["type"] == "idle":
                return result

    def test_record_waits_four_seconds_then_samples_relative_point_without_clicking(self):
        self.adapter.state = replace(self.adapter.state, foreground=False)
        self.adapter.on_wait = lambda adapter, _stopped: setattr(adapter, "state", replace(adapter.state, foreground=adapter.now >= 4.0))
        job_id = self.macro.record_point(self.adapter.window, "버튼", 750)
        events = self.completed_events(job_id)
        countdown = [event for event in events if event["type"] == "countdown"]
        self.assertEqual([event["remaining"] for event in countdown], [4, 3, 2, 1])
        self.assertTrue(all(event["mode"] == "recording" for event in countdown))
        recorded = next(event for event in events if event["type"] == "recorded")
        self.assertEqual(recorded["step"], ClickStep("버튼", 50, 80, 750))
        self.assertEqual(recorded["geometry"], self.adapter.geometry)
        self.assertEqual(recorded["browser"], "Chrome")
        self.assertEqual(self.adapter.clicks, [])
        self.assertGreaterEqual(self.adapter.now, 4.0)

    def test_record_rejects_cursor_outside_client_and_changed_profile_geometry(self):
        self.adapter.cursor = (50, 180)
        events = self.completed_events(self.macro.record_point(self.adapter.window, "영역 밖", 200))
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.adapter.cursor = (150, 180)
        self.adapter.state = replace(self.adapter.state, geometry=Geometry(900, 600, 96))
        events = self.completed_events(self.macro.record_point(self.adapter.window, "크기 변경", 200, self.profile))
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertEqual(self.adapter.clicks, [])

    def test_play_remaps_only_window_position_and_emits_progress_and_done(self):
        self.adapter.on_click = lambda adapter: setattr(adapter, "state", replace(adapter.state, left=500, top=300))
        events = self.completed_events(self.macro.play(self.adapter.window, self.profile))
        self.assertEqual(self.adapter.clicks, [(120, 140), (570, 390)])
        self.assertEqual([event["index"] for event in events if event["type"] == "progress"], [1, 2])
        self.assertEqual(next(event["count"] for event in events if event["type"] == "done"), 2)
        self.assertTrue(all(event["mode"] == "playing" for event in events if event["type"] == "countdown"))

    def test_focus_visibility_minimize_resize_dpi_or_identity_mismatch_sends_no_input(self):
        states = (
            replace(self.adapter.state, foreground=False), replace(self.adapter.state, visible=False),
            replace(self.adapter.state, minimized=True),
            replace(self.adapter.state, geometry=Geometry(801, 600, 96)),
            replace(self.adapter.state, geometry=Geometry(800, 600, 120)),
            replace(self.adapter.state, identity=replace(self.adapter.identity, pid=999)),
            replace(self.adapter.state, identity=replace(self.adapter.identity, created=999)),
            replace(self.adapter.state, identity=replace(self.adapter.identity, executable="msedge.exe")),
        )
        for state in states:
            with self.subTest(state=state):
                self.adapter.state = state
                events = self.completed_events(self.macro.play(self.adapter.window, self.profile))
                self.assertTrue(any(event["type"] == "error" for event in events))
                self.assertEqual(self.adapter.clicks, [])

    def test_losing_focus_after_one_action_prevents_next_click(self):
        self.adapter.on_click = lambda adapter: setattr(adapter, "state", replace(adapter.state, foreground=False))
        events = self.completed_events(self.macro.play(self.adapter.window, self.profile))
        self.assertEqual(self.adapter.clicks, [(120, 140)])
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "done" for event in events))

    def test_cancel_escape_corner_and_timeout_stop_during_countdown(self):
        for reason in ("cancel", "escape", "corner", "timeout"):
            with self.subTest(reason=reason):
                self.adapter.now = 0.0
                self.adapter.escape = False
                self.adapter.cursor = (150, 180)
                def interrupt(adapter, stopped):
                    if adapter.now < 1.0:
                        return
                    if reason == "cancel":
                        stopped.set()
                    elif reason == "escape":
                        adapter.escape = True
                    elif reason == "corner":
                        adapter.cursor = (0, 0)
                    else:
                        adapter.now = MAX_RUNTIME_SECONDS + 1
                self.adapter.on_wait = interrupt
                events = self.completed_events(self.macro.play(self.adapter.window, self.profile))
                self.assertTrue(any(event["type"] == "stopped" for event in events))
                self.assertFalse(any(event["type"] == "done" for event in events))
                self.assertEqual(self.adapter.clicks, [])

    def test_escape_between_actions_cancels_remaining_clicks(self):
        self.adapter.on_click = lambda adapter: setattr(adapter, "escape", True)
        events = self.completed_events(self.macro.play(self.adapter.window, self.profile))
        self.assertEqual(len(self.adapter.clicks), 1)
        self.assertTrue(any(event["type"] == "stopped" for event in events))

    def test_busy_job_is_rejected_and_close_cancels_the_active_thread(self):
        entered = threading.Event()
        def blocking_wait(seconds, stopped):
            entered.set()
            stopped.wait(1)
            self.adapter.now += seconds
        self.adapter.wait = blocking_wait
        job_id = self.macro.play(self.adapter.window, self.profile)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.macro.is_busy())
        with self.assertRaises(MacroError):
            self.macro.record_point(self.adapter.window, "동시 작업", 200)
        self.macro.close()
        events = self.completed_events(job_id)
        self.assertTrue(any(event["type"] == "stopped" for event in events))
        self.assertFalse(self.macro.is_busy())
        with self.assertRaises(MacroError):
            self.macro.play(self.adapter.window, self.profile)

    def test_wrong_browser_empty_playback_and_recording_limits_are_rejected_synchronously(self):
        with self.assertRaises(MacroError):
            self.macro.play(NativeBrowserWindow(1, "Wrong", "Edge"), self.profile)
        with self.assertRaises(MacroError):
            self.macro.play(self.adapter.window, MacroProfile("Chrome", self.adapter.geometry, ()))
        full = MacroProfile("Chrome", self.adapter.geometry, tuple(ClickStep(str(i), 1, 1, 200) for i in range(30)))
        with self.assertRaises(MacroError):
            self.macro.record_point(self.adapter.window, "31번째", 200, full)
        waiting = MacroProfile("Chrome", self.adapter.geometry, tuple(ClickStep(str(i), 1, 1, 30_000) for i in range(10)))
        with self.assertRaises(MacroError):
            self.macro.record_point(self.adapter.window, "초과", 200, waiting)
        self.assertFalse(self.macro.is_busy())

    def test_ie_is_mapped_and_file_explorer_is_excluded_without_native_enumeration(self):
        windows = [NativeBrowserWindow(1, "Chrome", "Chrome"), NativeBrowserWindow(2, "Edge", "Edge"),
                   NativeBrowserWindow(3, "IE", "iexplore"), NativeBrowserWindow(4, "Files", "explorer")]
        with patch("trip_capture.browser_macro.list_browser_windows", return_value=windows):
            self.assertEqual([window.browser for window in Win32Adapter().list_windows()], ["Chrome", "Edge", "Internet Explorer"])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        workspace_temp = Path(__file__).resolve().parents[1] / "artifacts" / "test-temp"
        workspace_temp.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=workspace_temp)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "profile.json"
        self.profile = MacroProfile("Edge", Geometry(800, 600, 144), (ClickStep("다음", 10, 20, 750),))

    def test_roundtrip_contains_only_public_profile_fields(self):
        save_profile(self.path, self.profile)
        self.assertEqual(load_profile(self.path), self.profile)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(value), {"version", "browser", "geometry", "steps"})
        for prohibited in ("hwnd", "pid", "title", "url", "password", "token", "executable"):
            self.assertNotIn(prohibited, self.path.read_text(encoding="utf-8"))

    def test_extra_actions_keys_and_non_integer_or_out_of_range_values_are_rejected(self):
        modifications = [
            lambda value: value.update(version=True), lambda value: value.update(version=2),
            lambda value: value.update(password="PRIVATE_TEST_VALUE"),
            lambda value: value["geometry"].update(pid=1),
            lambda value: value["geometry"].update(dpi=True),
            lambda value: value["steps"][0].update(action="type"),
            lambda value: value["steps"][0].update(x=True),
            lambda value: value["steps"][0].update(x=800),
            lambda value: value["steps"][0].update(wait_ms=-1),
            lambda value: value["steps"][0].update(name="line\nbreak"),
            lambda value: value.update(browser="explorer"),
        ]
        for change in modifications:
            value = self.profile.to_dict()
            change(value)
            with self.subTest(value=value), self.assertRaises(MacroError):
                MacroProfile.from_dict(value)

    def test_duplicate_json_keys_and_oversized_files_are_rejected(self):
        self.path.write_text('{"version":1,"version":1}', encoding="utf-8")
        with self.assertRaises(MacroError):
            load_profile(self.path)
        self.path.write_bytes(b" " * (MAX_PROFILE_BYTES + 1))
        with self.assertRaises(MacroError):
            load_profile(self.path)

    def test_step_count_and_total_wait_limits(self):
        geometry = Geometry(800, 600, 96)
        MacroProfile("Chrome", geometry, tuple(ClickStep(str(i), 1, 1, 10_000) for i in range(30)))
        with self.assertRaises(MacroError):
            MacroProfile("Chrome", geometry, tuple(ClickStep(str(i), 1, 1, 200) for i in range(31)))
        with self.assertRaises(MacroError):
            MacroProfile("Chrome", geometry, tuple(ClickStep(str(i), 1, 1, 30_000) for i in range(11)))
        for waiting in (0, 199, 30_001, 300_000):
            with self.subTest(waiting=waiting), self.assertRaises(MacroError):
                ClickStep("대기 범위", 1, 1, waiting)

    def test_packaged_selfcheck_uses_only_the_fake_adapter_and_clock(self):
        from trip_capture.macro_selfcheck import run_macro_selfcheck

        with patch("trip_capture.browser_macro.Win32Adapter", side_effect=AssertionError("Native adapter must not be constructed")), patch("trip_capture.browser_macro.list_browser_windows", side_effect=AssertionError("Native enumeration must not be used")):
            result = run_macro_selfcheck(self.path.parent / "selfcheck")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fake_clicks_sent"], 3)
        self.assertFalse(result["real_input_sent"])
        self.assertEqual(result["native_windows_calls"], 0)
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue((self.path.parent / "selfcheck" / "runtime_check.json").is_file())


class SendInputAdapterTests(unittest.TestCase):
    def adapter(self):
        adapter = Win32Adapter()
        user = Mock()
        user.SetThreadDpiAwarenessContext = None
        user.GetSystemMetrics.side_effect = {76: 0, 77: 0, 78: 1920, 79: 1080}.get
        user.GetForegroundWindow.return_value = 101
        user.WindowFromPoint.return_value = 404
        user.GetAncestor.return_value = 101
        user.GetAsyncKeyState.return_value = 0
        adapter._user, adapter._kernel = user, Mock()
        identity = WindowIdentity(101, 202, 303, "chrome.exe")
        geometry = Geometry(800, 600, 96)
        adapter.snapshot = Mock(return_value=WindowSnapshot(identity, geometry, 100, 100))
        return adapter, user, identity, geometry

    def test_sendinput_uses_one_move_down_up_batch_without_real_input(self):
        adapter, user, identity, geometry = self.adapter()
        user.SendInput.return_value = 3
        adapter.left_click(identity, geometry, 20, 40)
        self.assertEqual(user.SendInput.call_args.args[0], 3)
        inputs = user.SendInput.call_args.args[1]
        self.assertEqual([item.mi.dwFlags for item in inputs], [0xC001, 0x0002, 0x0004])

    def test_partial_send_releases_button_and_foreground_change_sends_nothing(self):
        adapter, user, identity, geometry = self.adapter()
        user.SendInput.side_effect = [2, 1]
        with self.assertRaises(MacroError):
            adapter.left_click(identity, geometry, 20, 40)
        self.assertEqual([call.args[0] for call in user.SendInput.call_args_list], [3, 1])
        adapter, user, identity, geometry = self.adapter()
        user.GetForegroundWindow.return_value = 999
        with self.assertRaises(MacroError):
            adapter.left_click(identity, geometry, 20, 40)
        user.SendInput.assert_not_called()

    def test_overlay_or_held_modifiers_or_mouse_button_sends_no_input(self):
        adapter, user, identity, geometry = self.adapter()
        user.GetAncestor.return_value = 999
        with self.assertRaises(MacroError):
            adapter.left_click(identity, geometry, 20, 40)
        user.SendInput.assert_not_called()
        for held in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x01):
            adapter, user, identity, geometry = self.adapter()
            user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == held else 0
            with self.subTest(held=held), self.assertRaises(MacroError):
                adapter.left_click(identity, geometry, 20, 40)
            user.SendInput.assert_not_called()

    def test_failed_dpi_context_stops_before_input(self):
        adapter, user, identity, geometry = self.adapter()
        user.SetThreadDpiAwarenessContext = Mock(return_value=None)
        with self.assertRaisesRegex(MacroError, "DPI"):
            adapter.left_click(identity, geometry, 20, 40)
        user.SendInput.assert_not_called()


if __name__ == "__main__":
    unittest.main()
