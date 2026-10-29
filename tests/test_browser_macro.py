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
        self.preparations = []

    def list_windows(self):
        return [self.window]

    def bind(self, window):
        return self.identity

    def snapshot(self, identity):
        return self.state

    def prepare_window(self, identity, mode, expected_geometry=None, *, stopped=None):
        self.preparations.append((identity, mode, expected_geometry))
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
        self.assertEqual(events[0]["type"], "preparing")
        self.assertEqual(events[0]["window_mode"], "maximize")
        self.assertEqual(recorded["step"], ClickStep("버튼", 50, 80, 750))
        self.assertEqual(recorded["geometry"], self.adapter.geometry)
        self.assertEqual(recorded["browser"], "Chrome")
        self.assertEqual(recorded["window_mode"], "maximize")
        self.assertEqual(self.adapter.preparations[0][1:], ("maximize", None))
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

    def test_new_record_rejects_geometry_change_during_countdown(self):
        self.adapter.on_wait = lambda adapter, _stopped: setattr(adapter, "state", replace(adapter.state, geometry=Geometry(900, 600, 96)))
        events = self.completed_events(self.macro.record_point(self.adapter.window, "크기 변경", 200))
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "recorded" for event in events))
        self.assertEqual(self.adapter.clicks, [])

    def test_recorded_profile_preserves_mode_and_supplies_expected_geometry(self):
        profile = replace(self.profile, window_mode="recorded")
        events = self.completed_events(self.macro.record_point(self.adapter.window, "기존 크기", 200, profile))
        self.assertEqual(self.adapter.preparations[-1][1:], ("recorded", profile.geometry))
        self.assertEqual(next(event["window_mode"] for event in events if event["type"] == "recorded"), "recorded")
        with self.assertRaises(MacroError):
            self.macro.record_point(self.adapter.window, "준비 방식 변경", 200, profile, window_mode="maximize")

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
        self.assertEqual(set(value), {"version", "browser", "geometry", "steps", "window_mode"})
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["window_mode"], "maximize")
        for prohibited in ("hwnd", "pid", "title", "url", "password", "token", "executable"):
            self.assertNotIn(prohibited, self.path.read_text(encoding="utf-8"))

    def test_extra_actions_keys_and_non_integer_or_out_of_range_values_are_rejected(self):
        modifications = [
            lambda value: value.update(version=True), lambda value: value.update(version=3),
            lambda value: value.update(password="PRIVATE_TEST_VALUE"),
            lambda value: value["geometry"].update(pid=1),
            lambda value: value["geometry"].update(dpi=True),
            lambda value: value["steps"][0].update(action="type"),
            lambda value: value["steps"][0].update(x=True),
            lambda value: value["steps"][0].update(x=800),
            lambda value: value["steps"][0].update(wait_ms=-1),
            lambda value: value["steps"][0].update(name="line\nbreak"),
            lambda value: value.update(browser="explorer"),
            lambda value: value.update(window_mode="resize-everything"),
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

    def test_v1_import_migrates_to_recorded_and_v2_requires_explicit_mode(self):
        old = self.profile.to_dict()
        old["version"] = 1
        old.pop("window_mode")
        legacy = MacroProfile.from_dict(old)
        self.assertEqual(legacy.window_mode, "recorded")
        self.assertEqual(legacy.to_dict()["version"], 2)
        self.assertEqual(legacy.to_dict()["window_mode"], "recorded")
        missing = self.profile.to_dict()
        missing.pop("window_mode")
        with self.assertRaises(MacroError):
            MacroProfile.from_dict(missing)
        old["window_mode"] = "maximize"
        with self.assertRaises(MacroError):
            MacroProfile.from_dict(old)
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


class WindowPreparationAdapterTests(unittest.TestCase):
    """Exercise native preparation logic against mocked Win32 functions only."""

    def adapter(self):
        adapter = Win32Adapter()
        user = Mock()
        user.SetThreadDpiAwarenessContext = None
        identity = WindowIdentity(101, 202, 303, "chrome.exe")
        data = {"geometry": Geometry(600, 400, 96), "left": 100, "top": 100,
                "zoomed": False, "iconic": False, "visible": True, "clock": 0.0,
                "pending": None, "resize_delay": 0.0, "apply_resize": True}
        work = (0, 0, 1920, 1040)
        adapter._user, adapter._kernel = user, Mock()
        adapter._identity = Mock(return_value=identity)
        adapter.escape_pressed = Mock(return_value=False)
        adapter.cursor_position = Mock(return_value=(200, 200))
        adapter.monotonic = lambda: data["clock"]
        def snapshot(_identity):
            return WindowSnapshot(identity, data["geometry"], data["left"], data["top"],
                                  data["visible"], data["iconic"], False)
        adapter.snapshot = Mock(side_effect=snapshot)
        user.IsIconic.side_effect = lambda _hwnd: data["iconic"]
        user.IsZoomed.side_effect = lambda _hwnd: data["zoomed"]
        user.IsWindowVisible.side_effect = lambda _hwnd: data["visible"]
        user.MonitorFromWindow.return_value = 303
        def monitor(_monitor, info):
            info._obj.rcWork.left, info._obj.rcWork.top, info._obj.rcWork.right, info._obj.rcWork.bottom = work
            return True
        user.GetMonitorInfoW.side_effect = monitor
        def show(hwnd, command):
            self.assertEqual(hwnd, identity.hwnd)
            data["iconic"] = False
            data["visible"] = True
            data["zoomed"] = command == 3
            data["geometry"] = Geometry(1920, 1040, 96) if command == 3 else Geometry(600, 400, 96)
            data["left"], data["top"] = (0, 0) if command == 3 else (100, 100)
            return 0  # ShowWindow zero is not a failure status.
        user.ShowWindowAsync.side_effect = show
        user.SetForegroundWindow.return_value = False  # OS foreground refusal is permitted before countdown.
        def outer(_hwnd, rectangle):
            rectangle._obj.left = data["left"] - 8
            rectangle._obj.top = data["top"] - 30
            rectangle._obj.right = rectangle._obj.left + data["geometry"].width + 16
            rectangle._obj.bottom = rectangle._obj.top + data["geometry"].height + 38
            return True
        user.GetWindowRect.side_effect = outer
        def apply_resize(arguments):
            hwnd, _after, left, top, width, height, _flags = arguments
            self.assertEqual(hwnd, identity.hwnd)
            self.assertTrue(work[0] <= left and work[1] <= top and left + width <= work[2] and top + height <= work[3])
            data["geometry"] = Geometry(width - 16, height - 38, data["geometry"].dpi)
            data["left"], data["top"] = left + 8, top + 30
            data["zoomed"] = False
        def resize(*arguments):
            if data["resize_delay"]:
                data["pending"] = (data["clock"] + data["resize_delay"], arguments)
            elif data["apply_resize"]:
                apply_resize(arguments)
            return True
        user.SetWindowPos.side_effect = resize
        def wait(seconds, stopped):
            data["clock"] += seconds
            if data["pending"] and data["clock"] >= data["pending"][0] and data["apply_resize"]:
                apply_resize(data["pending"][1])
                data["pending"] = None
        adapter.wait = wait
        return adapter, user, identity, data

    def test_maximize_restores_selected_minimized_window_without_requiring_foreground(self):
        adapter, user, identity, data = self.adapter()
        data["iconic"] = True
        result = adapter.prepare_window(identity, "maximize")
        self.assertEqual([call.args for call in user.ShowWindowAsync.call_args_list], [(101, 9), (101, 3)])
        self.assertEqual(result.geometry, Geometry(1920, 1040, 96))
        self.assertFalse(result.foreground)
        user.SetForegroundWindow.assert_called_once_with(101)
        user.SetWindowPos.assert_not_called()
        user.SendInput.assert_not_called()

    def test_recorded_mode_fits_exact_client_pixels_inside_work_area_with_async_flag(self):
        adapter, user, identity, _data = self.adapter()
        expected = Geometry(800, 600, 96)
        result = adapter.prepare_window(identity, "recorded", expected)
        self.assertEqual(result.geometry, expected)
        user.SetWindowPos.assert_called_once()
        self.assertTrue(user.SetWindowPos.call_args.args[-1] & 0x4000)
        self.assertEqual(user.SetWindowPos.call_args.args[0], identity.hwnd)
        user.SendInput.assert_not_called()

    def test_delayed_async_resize_waits_for_requested_client_size(self):
        adapter, user, identity, data = self.adapter()
        data["resize_delay"] = 0.5
        expected = Geometry(800, 600, 96)
        result = adapter.prepare_window(identity, "recorded", expected)
        self.assertEqual(result.geometry, expected)
        self.assertGreaterEqual(data["clock"], 0.7)
        self.assertLess(data["clock"], 3)
        user.SetWindowPos.assert_called_once()
        user.SendInput.assert_not_called()

    def test_unresponsive_resize_reaches_deadline_without_repeated_moves_or_clicks(self):
        adapter, user, identity, data = self.adapter()
        data["apply_resize"] = False
        with self.assertRaises(MacroError):
            adapter.prepare_window(identity, "recorded", Geometry(800, 600, 96))
        self.assertGreaterEqual(data["clock"], 3)
        user.SetWindowPos.assert_called_once()
        user.SendInput.assert_not_called()
        user.SetForegroundWindow.assert_not_called()

    def test_delayed_position_only_move_waits_until_window_is_inside_work_area(self):
        adapter, user, identity, data = self.adapter()
        expected = Geometry(800, 600, 96)
        data["geometry"] = expected
        data["left"] = -100
        data["resize_delay"] = 0.5
        result = adapter.prepare_window(identity, "recorded", expected)
        self.assertEqual(result.geometry, expected)
        self.assertGreaterEqual(result.left, 0)
        self.assertGreaterEqual(data["clock"], 0.7)
        self.assertLess(data["clock"], 3)
        user.SetWindowPos.assert_called_once()
        user.SendInput.assert_not_called()

    def test_legacy_maximized_geometry_uses_only_exact_maximize_fallback(self):
        adapter, user, identity, _data = self.adapter()
        expected = Geometry(1920, 1040, 96)
        result = adapter.prepare_window(identity, "recorded", expected)
        self.assertEqual(result.geometry, expected)
        user.ShowWindowAsync.assert_called_once_with(101, 3)
        user.SetWindowPos.assert_not_called()
        user.SendInput.assert_not_called()

    def test_oversized_or_dpi_mismatched_profiles_never_move_outside_monitor_or_click(self):
        for expected in (Geometry(20000, 10000, 96), Geometry(800, 600, 144)):
            adapter, user, identity, _data = self.adapter()
            with self.subTest(expected=expected), self.assertRaises(MacroError):
                adapter.prepare_window(identity, "recorded", expected)
            user.SetWindowPos.assert_not_called()
            user.SetForegroundWindow.assert_not_called()
            user.SendInput.assert_not_called()

    def test_identity_change_cancellation_and_unstable_rect_stop_preparation(self):
        adapter, user, identity, _data = self.adapter()
        adapter._identity.return_value = replace(identity, pid=999)
        with self.assertRaises(MacroError):
            adapter.prepare_window(identity, "maximize")
        user.ShowWindowAsync.assert_not_called()
        adapter, user, identity, _data = self.adapter()
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(MacroError):
            adapter.prepare_window(identity, "maximize", stopped=stopped)
        user.ShowWindowAsync.assert_not_called()
        adapter, user, identity, data = self.adapter()
        original_wait = adapter.wait
        def moving_wait(seconds, stopped):
            original_wait(seconds, stopped)
            data["left"] += 1
        adapter.wait = moving_wait
        with self.assertRaises(MacroError):
            adapter.prepare_window(identity, "maximize")
        self.assertGreaterEqual(data["clock"], 3)
        user.SetForegroundWindow.assert_not_called()
        user.SendInput.assert_not_called()

if __name__ == "__main__":
    unittest.main()
