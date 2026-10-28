from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trip_capture import native_capture as native


class NativeCaptureTests(unittest.TestCase):
    def test_bounds_include_negative_monitor_coordinates_and_reject_partial_window(self):
        native.validate_bounds((-1800, 10, -200, 900), (-1920, 0, 1920, 1080))
        for bounds in ((-1930, 0, 0, 900), (0, 0, 1921, 900), (0, 0, 500, 1081), (0, 0, 0, 10)):
            with self.subTest(bounds=bounds), self.assertRaises(RuntimeError):
                native.validate_bounds(bounds, (-1920, 0, 1920, 1080))

    def test_foreground_mismatch_does_not_read_screen_pixels(self):
        from PIL import ImageGrab

        user = Mock()
        user.IsWindow.return_value = True
        user.GetForegroundWindow.return_value = 999
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", return_value="Chrome"), patch.object(ImageGrab, "grab") as grab:
            with self.assertRaisesRegex(RuntimeError, "맨 앞"):
                native.save_visible_browser_window(native.NativeBrowserWindow(123, "영수증", "Chrome"), Path("unused.png"))
            grab.assert_not_called()

    def test_activation_restores_minimized_window_only(self):
        user = Mock()
        user.IsWindow.return_value = True
        user.IsIconic.return_value = False
        window = native.NativeBrowserWindow(123, "영수증", "Edge")
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", return_value="Edge"):
            native.activate_browser_window(window)
            user.ShowWindow.assert_not_called()
            user.SetForegroundWindow.assert_called_once_with(123)
            user.IsIconic.return_value = True
            native.activate_browser_window(window)
            user.ShowWindow.assert_called_once_with(123, 9)

    def test_foreground_switch_during_grab_does_not_save_pixels(self):
        from PIL import ImageGrab

        user = Mock()
        user.IsWindow.return_value = True
        user.GetForegroundWindow.side_effect = [123, 123, 999]
        user.SetThreadDpiAwarenessContext = None
        user.GetSystemMetrics.side_effect = {76: 0, 77: 0, 78: 1920, 79: 1080}.get
        def rectangle(_hwnd, result):
            result._obj.left, result._obj.top, result._obj.right, result._obj.bottom = 10, 10, 500, 500
            return True
        user.GetWindowRect.side_effect = rectangle
        dwm = Mock()
        dwm.DwmGetWindowAttribute.return_value = 1
        screenshot = Mock()
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", return_value="Chrome"), patch.object(native.ctypes, "WinDLL", return_value=dwm), patch.object(ImageGrab, "grab", return_value=screenshot):
            with self.assertRaisesRegex(RuntimeError, "다른 창으로 전환"):
                native.save_visible_browser_window(native.NativeBrowserWindow(123, "영수증", "Chrome"), Path("unused.png"))
        screenshot.save.assert_not_called()
        screenshot.close.assert_called_once()

    def test_dialog_requires_explicit_window_selection_and_restores_after_failure(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        dialog = None
        windows = [native.NativeBrowserWindow(123, "영수증 - Chrome", "Chrome")]
        callback = Mock()
        try:
            with patch.object(native, "list_browser_windows", return_value=windows) as enumerate_windows:
                dialog = native.NativeCaptureDialog(root, callback)
            enumerate_windows.assert_called_once_with(include_other_windows=True)
            self.assertTrue(dialog.include_other_windows.get())
            self.assertEqual(dialog.selection.get(), "")
            self.assertTrue(dialog.capture_button.instate(["disabled"]))
            dialog.selection.set(next(iter(dialog._windows)))
            dialog._sync_button()
            self.assertFalse(dialog.capture_button.instate(["disabled"]))
            with patch.object(dialog, "after") as after:
                dialog._capture()
            self.assertTrue(dialog._busy)
            after.assert_called_once()
            self.assertEqual(after.call_args.args[0], 700)
            with patch.object(root, "deiconify"), patch.object(dialog, "deiconify"), patch.object(dialog, "lift"), patch.object(native, "activate_browser_window", side_effect=RuntimeError("창을 확인하세요")):
                dialog._activate(windows[0])
            self.assertFalse(dialog._busy)
            self.assertEqual(dialog.message.get(), "창을 확인하세요")
            self.assertFalse(dialog.capture_button.instate(["disabled"]))
            callback.assert_not_called()
        finally:
            root.destroy()

    def test_saved_screen_callback_uses_staging_path_without_live_capture(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        window = native.NativeBrowserWindow(123, "하이패스 영수증", "Chrome")
        callback = Mock()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(native, "list_browser_windows", return_value=[window]), patch.object(native, "app_data_dir", return_value=Path(directory)), patch.object(native, "save_visible_browser_window") as save, patch.object(root, "deiconify"):
                dialog = native.NativeCaptureDialog(root, callback)
                dialog._save(window)
                path, title = callback.call_args.args
                self.assertEqual(path.parent, Path(directory) / "staging")
                self.assertTrue(path.name.startswith("native-"))
                self.assertEqual(path.suffix, ".png")
                self.assertEqual(title, "하이패스 영수증")
                save.assert_called_once_with(window, path)
        finally:
            root.destroy()

    def test_user_has_time_to_focus_browser_after_activation_request(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            with patch.object(native, "list_browser_windows", return_value=[]):
                dialog = native.NativeCaptureDialog(root, Mock())
            with patch.object(native, "activate_browser_window"), patch.object(dialog, "after") as after:
                dialog._activate(native.NativeBrowserWindow(123, "영수증", "Chrome"))
                self.assertEqual(after.call_args.args[0], 3000)
        finally:
            root.destroy()

    def test_other_applications_require_explicit_scope_and_exclude_shell_and_own_windows(self):
        user = Mock()
        titles = {1: "Chrome receipt", 2: "Acrobat receipt", 3: "Hancom print", 4: "Hidden", 5: "Desktop", 6: "", 7: "This app"}
        applications = {1: "Chrome", 2: "Acrobat", 3: "HwpViewer", 4: "Other", 5: "explorer", 6: "Other", 7: "python"}
        user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 4
        user.GetWindowTextLengthW.side_effect = lambda hwnd: len(titles[hwnd])
        def process_id(hwnd, result):
            result._obj.value = native.os.getpid() if hwnd == 7 else 50000 + hwnd
            return 1
        def window_class(hwnd, result, _length):
            result.value = "WorkerW" if hwnd == 5 else "ApplicationWindow"
            return len(result.value)
        def window_title(hwnd, result, _length):
            result.value = titles[hwnd]
            return len(result.value)
        def enumerate_windows(callback, _parameter):
            for hwnd in titles:
                callback(hwnd, 0)
            return True
        user.GetWindowThreadProcessId.side_effect = process_id
        user.GetClassNameW.side_effect = window_class
        user.GetWindowTextW.side_effect = window_title
        user.EnumWindows.side_effect = enumerate_windows
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", side_effect=lambda hwnd, *_args: applications[hwnd]):
            self.assertEqual([window.hwnd for window in native.list_browser_windows()], [1])
            windows = native.list_browser_windows(include_other_windows=True)
            self.assertEqual({window.hwnd for window in windows}, {1, 2, 3})
            self.assertEqual({window.browser for window in windows}, {"Chrome", "Acrobat", "HwpViewer"})

    def test_pdf_viewer_identity_must_still_match_when_activated(self):
        user = Mock()
        user.IsWindow.return_value = True
        user.IsIconic.return_value = False
        selected = native.NativeBrowserWindow(123, "영수증.pdf", "Acrobat")
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", return_value="Acrobat"):
            native.activate_browser_window(selected)
            user.SetForegroundWindow.assert_called_once_with(123)
        with patch.object(native, "_apis", return_value=(user, Mock())), patch.object(native, "_application_name", return_value="OtherApplication"):
            with self.assertRaisesRegex(RuntimeError, "닫혔습니다"):
                native.activate_browser_window(selected)


if __name__ == "__main__":
    unittest.main()
