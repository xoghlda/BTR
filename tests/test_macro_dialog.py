"""Tk controls and job events only: the macro backend never sends real input."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from trip_capture import macro_dialog
from trip_capture.browser_macro import BrowserMacro, ClickStep, Geometry, MacroProfile
from trip_capture.native_capture import NativeBrowserWindow


class MacroDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = None
        self.dialog = None
        self.core = Mock(spec=BrowserMacro)
        self.window = NativeBrowserWindow(1234, "개발용 가상 브라우저 창", "Chrome")
        self.core.list_windows.return_value = [self.window]
        self.core.is_busy.return_value = False
        self.core.record_point.return_value = "record-job"
        self.core.play.return_value = "play-job"
        self.busy_callback = Mock()
        self.close_callback = Mock()
        self.backend_patch = patch.object(macro_dialog, "BrowserMacro", return_value=self.core)
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)
        self.addCleanup(self._cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.dialog = macro_dialog.BrowserMacroDialog(self.root, self.busy_callback, self.close_callback)
        self.dialog.withdraw()
        self.root.update_idletasks()

    def _cleanup(self):
        if self.dialog is not None:
            self.dialog.close()
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def select_window(self):
        self.dialog.selection.set(next(iter(self.dialog._windows)))
        self.dialog._window_changed()

    def profile(self):
        return MacroProfile("Chrome", Geometry(1200, 800, 96), (
            ClickStep("조회", 100, 150, 1000), ClickStep("영수증", 240, 300, 2000),
        ))

    def event(self, kind, *, job_id="record-job", **values):
        self.dialog._handle_event({"type": kind, "job_id": job_id, **values})

    def test_existing_browser_must_be_explicitly_selected_and_empty_macro_cannot_play(self):
        dialog = self.dialog
        self.assertEqual(dialog.selection.get(), "")
        self.assertTrue(dialog.record_button.instate(["disabled"]))
        self.assertTrue(dialog.play_button.instate(["disabled"]))
        self.assertTrue(dialog.stop_button.instate(["disabled"]))
        self.select_window()
        self.assertFalse(dialog.record_button.instate(["disabled"]))
        self.assertTrue(dialog.play_button.instate(["disabled"]))
        self.assertIn("브라우저 버튼 매크로", dialog.title())
        self.assertLessEqual(dialog.winfo_reqwidth(), 800)
        self.assertLessEqual(dialog.winfo_reqheight(), 620)

    def test_record_notifies_busy_before_backend_and_restores_only_after_idle(self):
        self.select_window()
        dialog = self.dialog
        dialog.step_name.set("조회 버튼")
        dialog.wait_seconds.set("1.5")

        def start(window, name, wait_ms, *, profile):
            self.busy_callback.assert_called_once_with(True)
            self.assertEqual(dialog.state(), "withdrawn")
            self.assertTrue(dialog.is_busy())
            self.assertIs(window, self.window)
            self.assertEqual((name, wait_ms, profile), ("조회 버튼", 1500, None))
            return "record-job"

        self.core.record_point.side_effect = start
        dialog._record()
        self.assertTrue(dialog.import_button.instate(["disabled"]))
        self.assertTrue(dialog.play_button.instate(["disabled"]))
        self.assertFalse(dialog.stop_button.instate(["disabled"]))
        step, geometry = ClickStep("조회 버튼", 100, 160, 1500), Geometry(1200, 800, 96)
        with patch.object(dialog, "_restore") as restore:
            self.event("countdown", mode="recording", remaining=3)
            self.event("recorded", step=step, geometry=geometry, browser="Chrome")
            restore.assert_not_called()
            self.assertEqual(dialog.profile.steps, (step,))
            self.assertTrue(dialog.is_busy())
            self.event("idle")
            restore.assert_called_once()
        self.assertFalse(dialog.is_busy())
        self.assertEqual([call.args[0] for call in self.busy_callback.call_args_list], [True, False])

    def test_unrelated_and_duplicate_job_events_do_not_append_or_unlock(self):
        self.select_window()
        self.dialog._record()
        values = {"step": ClickStep("조회", 50, 80, 1000), "geometry": Geometry(1200, 800, 96), "browser": "Chrome"}
        self.event("recorded", job_id="stale-job", **values)
        self.event("idle", job_id="stale-job")
        self.assertIsNone(self.dialog.profile)
        self.assertTrue(self.dialog.is_busy())
        self.event("recorded", **values)
        self.event("recorded", **values)
        self.assertEqual(len(self.dialog.profile.steps), 1)
        self.event("idle")
        self.event("idle")
        self.assertEqual(self.busy_callback.call_count, 2)

    def test_play_and_escape_request_stop_without_claiming_receipt_success(self):
        self.select_window()
        dialog = self.dialog
        dialog.profile = self.profile()
        dialog._render_steps()
        dialog._play()
        self.core.play.assert_called_once_with(self.window, dialog.profile)
        self.assertEqual(dialog._escape(), "break")
        self.core.cancel.assert_called_once()
        self.assertTrue(dialog.is_busy())
        self.event("progress", job_id="play-job", index=1, total=2, name="조회")
        self.event("done", job_id="play-job", count=2)
        self.assertIn("직접 확인", dialog.message.get())
        self.assertNotIn("영수증 저장 완료", dialog.message.get())
        self.event("idle", job_id="play-job")
        self.assertFalse(dialog.is_busy())

    def test_backend_start_error_restores_controls_and_parent_callback(self):
        self.select_window()
        self.core.record_point.side_effect = ValueError("개발용 창 확인 오류")
        with patch.object(self.dialog, "_restore") as restore:
            self.dialog._record()
        restore.assert_called_once()
        self.assertFalse(self.dialog.is_busy())
        self.assertIsNone(self.dialog._job_id)
        self.assertIn("창 확인 오류", self.dialog.message.get())
        self.assertEqual([call.args[0] for call in self.busy_callback.call_args_list], [True, False])

    def test_editor_changes_only_name_wait_and_explicit_order(self):
        dialog = self.dialog
        dialog.profile = self.profile()
        dialog._render_steps(0)
        dialog.step_name.set("수정된 조회")
        dialog.wait_seconds.set("0.2")
        dialog._update_step()
        self.assertEqual(dialog.profile.steps[0], ClickStep("수정된 조회", 100, 150, 200))
        dialog._move_step(1)
        self.assertEqual(dialog.profile.steps[1].name, "수정된 조회")
        dialog._remove_step()
        self.assertEqual([step.name for step in dialog.profile.steps], ["영수증"])
        self.core.play.assert_not_called()

    def test_invalid_delay_cannot_start_backend(self):
        self.select_window()
        for value in ("0.1", "30.1", "NaN", "Infinity", "", "abc"):
            with self.subTest(value=value):
                self.dialog.wait_seconds.set(value)
                self.dialog._record()
                self.assertIn("0.2~30초", self.dialog.message.get())
        self.core.record_point.assert_not_called()
        self.busy_callback.assert_not_called()

    def test_import_is_explicit_does_not_select_window_or_start_job(self):
        profile = self.profile()
        with patch.object(macro_dialog.filedialog, "askopenfilename", return_value="development.json"), \
             patch.object(macro_dialog, "load_profile", return_value=profile) as load:
            self.dialog._import_profile()
        load.assert_called_once_with(Path("development.json"))
        self.assertIs(self.dialog.profile, profile)
        self.assertEqual(self.dialog.selection.get(), "")
        self.assertTrue(self.dialog.play_button.instate(["disabled"]))
        self.core.play.assert_not_called()
        self.core.record_point.assert_not_called()
        self.busy_callback.assert_not_called()

    def test_export_passes_only_profile_to_backend_serializer(self):
        profile = self.profile()
        self.dialog.profile = profile
        with patch.object(macro_dialog.filedialog, "asksaveasfilename", return_value="development.json") as choose, \
             patch.object(macro_dialog, "save_profile") as save:
            self.dialog._export_profile()
        save.assert_called_once_with(Path("development.json"), profile)
        self.assertEqual(choose.call_args.kwargs["initialfile"], "browser-buttons.btr-macro.json")
        self.assertEqual(choose.call_args.kwargs["defaultextension"], ".btr-macro.json")
        self.core.play.assert_not_called()

    def test_invalid_recorded_geometry_preserves_profile_and_waits_for_idle(self):
        self.select_window()
        dialog = self.dialog
        existing = self.profile()
        dialog.profile = existing
        dialog._record()
        with patch.object(dialog, "_restore") as restore:
            self.event("recorded", step=ClickStep("추가", 10, 20, 200), geometry=None, browser="Chrome")
            self.assertIs(dialog.profile, existing)
            self.assertIn("등록하지 못했습니다", dialog.message.get())
            self.assertTrue(dialog.is_busy())
            restore.assert_not_called()
            self.event("idle")
            restore.assert_called_once()
        self.assertFalse(dialog.is_busy())

    def test_malformed_event_does_not_stop_polling_or_hide_the_idle_window(self):
        self.select_window()
        dialog = self.dialog
        dialog._record()
        dialog.after_cancel(dialog._poll_after_id)
        dialog._poll_after_id = None
        dialog.events.put(None)
        dialog.events.put({"type": "idle", "job_id": "record-job"})
        with patch.object(dialog, "_restore") as restore:
            dialog._poll_events()
        restore.assert_called_once()
        self.assertFalse(dialog.is_busy())
        self.assertIn("표시하지 못했습니다", dialog.message.get())
        self.assertIsNotNone(dialog._poll_after_id)

    def test_bad_import_keeps_existing_profile(self):
        existing = self.profile()
        self.dialog.profile = existing
        with patch.object(macro_dialog.filedialog, "askopenfilename", return_value="invalid.json"), \
             patch.object(macro_dialog, "load_profile", side_effect=ValueError("잘못된 매크로")), \
             patch.object(macro_dialog.messagebox, "showerror") as error:
            self.dialog._import_profile()
        self.assertIs(self.dialog.profile, existing)
        error.assert_called_once()

    def test_busy_actions_cannot_mutate_profile_or_open_files(self):
        dialog = self.dialog
        self.select_window()
        dialog.profile = self.profile()
        dialog._render_steps(0)
        before = dialog.profile
        dialog._play()
        with patch.object(macro_dialog.filedialog, "askopenfilename") as load, \
             patch.object(macro_dialog.filedialog, "asksaveasfilename") as save:
            dialog._new_profile()
            dialog._remove_step()
            dialog._move_step(1)
            dialog._update_step()
            dialog._import_profile()
            dialog._export_profile()
            dialog._record()
            dialog._play()
        load.assert_not_called()
        save.assert_not_called()
        self.assertIs(dialog.profile, before)
        self.core.play.assert_called_once()
        self.core.record_point.assert_not_called()

    def test_close_cancels_poll_and_reports_backend_busy_after_tk_destroy(self):
        self.select_window()
        dialog = self.dialog
        dialog._record()
        self.core.is_busy.return_value = True
        dialog.close()
        self.core.close.assert_called_once()
        self.assertTrue(dialog.is_busy())
        self.assertIsNone(dialog._poll_after_id)
        self.event("recorded", step=ClickStep("stale", 20, 20, 200), geometry=Geometry(1200, 800, 96), browser="Chrome")
        dialog._poll_events()
        self.assertIsNone(dialog.profile)
        self.assertIsNone(dialog._poll_after_id)
        dialog.close()
        self.close_callback.assert_called_once()
        self.assertEqual([call.args[0] for call in self.busy_callback.call_args_list], [True, False])
        self.core.is_busy.return_value = False
        self.assertFalse(dialog.is_busy())


if __name__ == "__main__":
    unittest.main()
