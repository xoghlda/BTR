"""Workflow widgets use mocked accounts, collector and engine; no native input."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from trip_capture import workflow_dialog as ui
from trip_capture.browser_macro import ClickStep, Geometry, MacroProfile
from trip_capture.models import RunRequest
from trip_capture.native_capture import NativeBrowserWindow
from trip_capture.workflow import WorkflowEngine, WorkflowProfile, WorkflowStep


class WorkflowDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = self.dialog = None
        self.engine = Mock(spec=WorkflowEngine)
        self.engine.list_windows.return_value = [NativeBrowserWindow(123, "개발용 가상 창", "Chrome")]
        self.engine.is_busy.return_value = False
        self.engine.record_point.return_value = "record-job"
        self.engine.play.return_value = "play-job"
        self.engine.resume.return_value = True
        self.store = Mock()
        self.store.get.return_value = None
        self.collector = Mock()
        self.busy = Mock(); self.closed = Mock(); self.report = Mock()
        for attribute, replacement in (("WorkflowEngine", Mock(return_value=self.engine)),
                                       ("WindowsCredentialStore", Mock(return_value=self.store)),
                                       ("WorkflowEvidence", Mock(return_value=self.collector))):
            patcher = patch.object(ui, attribute, replacement)
            patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        from trip_capture.gui import TripCaptureApp
        TripCaptureApp._build_styles(SimpleNamespace(root=self.root))
        self.request = RunRequest("2026-10-20", "개발 출발지", "개발 출장지", sites=("korail",), transport_mode="train",
                                  trip_end_date="2026-10-21", rail_origin="오송", rail_destination="서울")
        self.dialog = ui.WorkflowDialog(self.root, self.busy, self.closed, request=self.request, on_report=self.report)
        self.dialog.withdraw()
        self.root.update_idletasks()

    def cleanup(self):
        if self.dialog is not None and not self.dialog._closed:
            if self.dialog._busy:
                self.dialog._handle_event({"type": "idle", "job_id": self.dialog._job_id})
            self.dialog.close()
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def select_window(self):
        self.dialog.selection.set(next(iter(self.dialog._windows)))
        self.dialog._sync()

    def profile(self, steps=None, *, geometry=True, site="korail", mode="maximize"):
        return WorkflowProfile("Chrome", Geometry(1200, 800, 96) if geometry else None,
                               tuple(steps or [WorkflowStep("capture", "원본 화면", site)]), mode)

    def event(self, kind, job="play-job", **values):
        self.dialog._handle_event({"type": kind, "job_id": job, **values})

    def test_opening_is_memory_only_and_window_selection_is_explicit(self):
        ui.WindowsCredentialStore.assert_not_called()
        ui.WorkflowEvidence.assert_not_called()
        self.store.get.assert_not_called()
        self.assertEqual(self.dialog.selection.get(), "")
        self.assertTrue(self.dialog.play_button.instate(["disabled"]))
        self.assertEqual(len(self.dialog.notebook.tabs()), 3)
        self.assertEqual(set(self.dialog._variables()), {
            "trip_date", "trip_end_date", "trip_date_compact", "trip_end_date_compact",
            "origin", "destination", "rail_origin", "rail_destination",
        })

    def test_full_train_template_is_inert_and_all_coordinates_are_unregistered(self):
        self.dialog._template()
        profile = self.dialog.profile
        self.assertIsNone(profile.geometry)
        self.assertTrue({"open_url", "username", "password", "pause", "text", "click", "capture"} <= {step.kind for step in profile.steps})
        self.assertEqual({step.site for step in profile.steps}, {"korail"})
        self.assertTrue(all((step.x, step.y) == (-1, -1) for step in profile.steps if step.kind in ui.POINT_KINDS))
        self.assertTrue(all(step.value == "" for step in profile.steps if step.kind in ("username", "password")))
        self.select_window()
        self.dialog.profile = WorkflowProfile(profile.browser, Geometry(1200, 800, 96), profile.steps, profile.window_mode)
        self.dialog._play()
        self.assertIn("미등록", self.dialog.message.get())
        self.engine.play.assert_not_called()

    def test_car_template_respects_only_selected_sources(self):
        self.dialog.request = RunRequest("2026-10-20", "출발지", "도착지", sites=("naver", "hipass"), transport_mode="car")
        self.dialog._template()
        self.assertEqual({step.site for step in self.dialog.profile.steps}, {"naver", "hipass"})
        self.assertEqual(sum(step.kind == "capture" for step in self.dialog.profile.steps), 2)
        self.assertTrue(any(step.kind == "username" and step.site == "hipass" for step in self.dialog.profile.steps))

    def test_record_converts_clickstep_into_selected_workflow_type_without_typing(self):
        self.select_window()
        self.dialog._template()
        index = next(i for i, step in enumerate(self.dialog.profile.steps) if step.kind == "username")
        self.dialog.tree.selection_set(str(index)); self.dialog._selected()
        self.dialog._record()
        self.assertIsNone(self.engine.record_point.call_args.kwargs["profile"])
        self.event("recorded", job="record-job", step=ClickStep("위치", 120, 220, 1000),
                   geometry=Geometry(1200, 800, 96), browser="Chrome", window_mode="maximize")
        step = self.dialog.profile.steps[index]
        self.assertEqual((step.kind, step.x, step.y, step.value), ("username", 120, 220, ""))
        self.engine.play.assert_not_called()
        self.event("idle", job="record-job")

    def test_subsequent_record_uses_macro_geometry_guard_not_workflow_profile(self):
        self.select_window()
        self.dialog.profile = self.profile([WorkflowStep("text", "날짜", "korail", 20, 30, "{trip_date}")], mode="recorded")
        self.dialog._render(0); self.dialog._selected(); self.dialog._record()
        guard = self.engine.record_point.call_args.kwargs["profile"]
        self.assertIsInstance(guard, MacroProfile)
        self.assertNotIsInstance(guard, WorkflowProfile)
        self.assertEqual(guard.steps, ())
        self.assertEqual(guard.window_mode, "recorded")

    def test_play_injects_memory_accounts_variables_and_fresh_evidence_collector(self):
        self.select_window()
        self.dialog.profile = self.profile([
            WorkflowStep("username", "아이디", "korail", 30, 40),
            WorkflowStep("password", "암호", "korail", 60, 70), WorkflowStep("capture", "영수증", "korail"),
        ])
        self.dialog.accounts["korail"]["username"].set("PRIVATE_LOGIN_ID")
        self.dialog.accounts["korail"]["password"].set("PRIVATE_PASSWORD")
        self.dialog._play()
        ui.WorkflowEvidence.assert_called_once_with(self.request)
        self.assertIs(ui.WorkflowEngine.call_args.kwargs["collector"], self.collector)
        arguments = self.engine.play.call_args.kwargs
        self.assertEqual(arguments["accounts"]["korail"], ("PRIVATE_LOGIN_ID", "PRIVATE_PASSWORD"))
        self.assertEqual(arguments["variables"]["trip_end_date_compact"], "20261021")
        self.assertEqual(arguments["pdf_paths"], {})
        self.store.get.assert_not_called(); self.store.set.assert_not_called()
        self.assertEqual([call.args[0] for call in self.busy.call_args_list], [True])

    def test_missing_accounts_and_unselected_sources_are_blocked_before_engine(self):
        self.select_window()
        self.dialog.profile = self.profile([WorkflowStep("password", "암호", "korail", 30, 40)])
        self.dialog._play()
        self.assertIn("계정", self.dialog.message.get())
        self.dialog.profile = self.profile(site="hipass")
        self.dialog._play()
        self.assertIn("선택한 사이트 밖", self.dialog.message.get())
        self.engine.play.assert_not_called()

    def test_no_request_allows_editing_but_blocks_execution(self):
        self.select_window(); self.dialog.profile = self.profile(); self.dialog.request = None
        self.dialog._play()
        self.assertIn("편집만", self.dialog.message.get())
        self.engine.play.assert_not_called()

    def test_pause_preserves_busy_and_resume_hides_without_a_new_job(self):
        self.select_window(); self.dialog.profile = self.profile(); self.dialog._play()
        self.event("paused", message="인증을 완료하세요.")
        self.assertTrue(self.dialog.is_busy())
        self.assertTrue(self.dialog._paused)
        self.assertFalse(self.dialog.resume_button.instate(["disabled"]))
        self.assertTrue(self.dialog.value_entry.instate(["disabled"]))
        self.dialog._resume()
        self.engine.resume.assert_called_once_with("play-job")
        self.assertFalse(self.dialog._paused)
        self.assertEqual(self.dialog.state(), "withdrawn")
        self.assertEqual(self.busy.call_count, 1)
        self.event("idle")
        self.assertEqual([call.args[0] for call in self.busy.call_args_list], [True, False])

    def test_failed_resume_returns_to_paused_controls(self):
        self.select_window(); self.dialog.profile = self.profile(); self.dialog._play()
        self.event("paused", message="인증")
        self.engine.resume.return_value = False
        self.dialog._resume()
        self.assertTrue(self.dialog._paused)
        self.assertTrue(self.dialog._busy)
        self.assertIn("계속 실행을 전달하지 못했습니다", self.dialog.message.get())

    def test_error_report_is_forwarded_once_and_login_values_are_redacted(self):
        self.select_window(); self.dialog.profile = self.profile()
        self.dialog.accounts["korail"]["username"].set("PRIVATE_ID")
        self.dialog.accounts["korail"]["password"].set("PRIVATE_PASSWORD")
        self.dialog._play()
        self.event("error", report="partial-report.html", message="PRIVATE_ID PRIVATE_PASSWORD")
        self.event("error", report="partial-report.html", message="PRIVATE_ID PRIVATE_PASSWORD")
        self.report.assert_called_once_with(Path("partial-report.html"))
        self.assertNotIn("PRIVATE", self.dialog.message.get())

    def test_close_cancels_then_waits_for_partial_report_and_idle_before_clearing_accounts(self):
        self.select_window(); self.dialog.profile = self.profile(); self.dialog._play()
        self.dialog.accounts["korail"]["password"].set("PRIVATE_PASSWORD")
        self.dialog.close()
        self.assertTrue(self.dialog._closing)
        self.assertFalse(self.dialog._closed)
        self.engine.cancel.assert_called_once()
        self.event("stopped", report="partial.html", message="중지")
        self.event("idle")
        self.assertTrue(self.dialog._closed)
        self.report.assert_called_once_with(Path("partial.html"))
        self.closed.assert_called_once()
        self.assertEqual(self.dialog.accounts["korail"]["password"].get(), "")
        self.dialog._handle_event({"type": "paused", "job_id": "play-job"})

    def test_account_load_save_delete_use_fixed_keys_only_after_explicit_action(self):
        self.dialog.accounts["hipass"]["username"].set("PRIVATE_ID")
        self.dialog.accounts["hipass"]["password"].set("PRIVATE_PASSWORD")
        self.dialog._save_account("hipass")
        self.assertEqual({call.args for call in self.store.set.call_args_list}, {
            ("workflow:hipass:username", "PRIVATE_ID"), ("workflow:hipass:password", "PRIVATE_PASSWORD"),
        })
        self.assertEqual([call.args[0] for call in self.store.get.call_args_list], ["workflow:hipass:username", "workflow:hipass:password"])
        self.store.get.reset_mock()
        self.store.get.side_effect = ["LOADED_ID", "LOADED_PASSWORD"]
        self.dialog._load_account("hipass")
        self.assertEqual([call.args[0] for call in self.store.get.call_args_list], ["workflow:hipass:username", "workflow:hipass:password"])
        self.dialog._delete_account("hipass")
        self.assertEqual({call.args[0] for call in self.store.delete.call_args_list}, {"workflow:hipass:username", "workflow:hipass:password"})

    def test_account_storage_error_never_displays_the_credential_value(self):
        self.dialog.accounts["hipass"]["username"].set("PRIVATE_ID")
        self.dialog.accounts["hipass"]["password"].set("PRIVATE_PASSWORD")
        self.store.set.side_effect = RuntimeError("PRIVATE_PASSWORD")
        self.dialog._save_account("hipass")
        self.assertNotIn("PRIVATE", self.dialog.account_status["hipass"].get())

    def test_partial_account_save_rolls_back_both_previously_saved_values(self):
        self.dialog.accounts["hipass"]["username"].set("NEW_PRIVATE_ID")
        self.dialog.accounts["hipass"]["password"].set("NEW_PRIVATE_PASSWORD")
        self.store.get.side_effect = ["OLD_PRIVATE_ID", "OLD_PRIVATE_PASSWORD"]
        self.store.set.side_effect = [None, RuntimeError("NEW_PRIVATE_PASSWORD"), None, None]
        self.dialog._save_account("hipass")
        self.assertEqual([call.args for call in self.store.set.call_args_list][-2:], [
            ("workflow:hipass:username", "OLD_PRIVATE_ID"), ("workflow:hipass:password", "OLD_PRIVATE_PASSWORD"),
        ])
        self.store.delete.assert_not_called()
        self.assertNotIn("PRIVATE", self.dialog.account_status["hipass"].get())

    def test_account_backup_read_failure_never_starts_writing(self):
        self.dialog.accounts["hipass"]["username"].set("PRIVATE_ID")
        self.dialog.accounts["hipass"]["password"].set("PRIVATE_PASSWORD")
        self.store.get.side_effect = RuntimeError("PRIVATE_PASSWORD")
        self.dialog._save_account("hipass")
        self.store.set.assert_not_called(); self.store.delete.assert_not_called()
        self.assertNotIn("PRIVATE", self.dialog.account_status["hipass"].get())

    def test_username_password_values_cannot_enter_workflow_json(self):
        self.dialog.kind.set(ui.KINDS["password"])
        self.dialog.value.set("PRIVATE_PASSWORD"); self.dialog._kind_changed()
        self.assertEqual(self.dialog.value.get(), "")
        self.assertTrue(self.dialog.value_entry.instate(["disabled"]))
        self.dialog._template()
        self.dialog.accounts["korail"]["username"].set("PRIVATE_ID")
        self.dialog.accounts["korail"]["password"].set("PRIVATE_PASSWORD")
        with patch.object(ui.filedialog, "asksaveasfilename", return_value="fixture.btr-workflow.json"), patch.object(ui, "save_workflow") as save:
            self.dialog._export()
        exported = json.dumps(save.call_args.args[1].to_dict(), ensure_ascii=False)
        self.assertNotIn("PRIVATE", exported)
        self.assertIsNone(save.call_args.args[1].geometry)
        self.engine.play.assert_not_called()

    def test_known_secret_in_literal_step_value_is_blocked_from_export(self):
        self.dialog.profile = self.profile([WorkflowStep("text", "입력", "korail", 10, 20, "PRIVATE_PASSWORD")])
        self.dialog.accounts["korail"]["password"].set("PRIVATE_PASSWORD")
        with patch.object(ui.filedialog, "asksaveasfilename") as chooser:
            self.dialog._export()
        chooser.assert_not_called()
        self.assertNotIn("PRIVATE_PASSWORD", self.dialog.message.get())

    def test_pdf_requires_explicit_exact_file_path_before_play(self):
        self.select_window(); self.dialog.profile = self.profile([WorkflowStep("pdf", "새 PDF", "korail", value="receipt_pdf")])
        self.dialog._play()
        self.assertIn("정확한 출력 PDF", self.dialog.message.get())
        self.engine.play.assert_not_called()
        self.dialog.pdf_path.set(str(Path.cwd() / "artifacts" / "future-receipt.pdf"))
        self.dialog._play()
        self.assertEqual(set(self.engine.play.call_args.kwargs["pdf_paths"]), {"receipt_pdf"})

    def test_update_kind_invalidates_old_point_and_reorder_preserves_mode(self):
        self.dialog.profile = self.profile([WorkflowStep("click", "클릭", "korail", 50, 70), WorkflowStep("capture", "원본", "korail")], mode="recorded")
        self.dialog._render(0); self.dialog._selected()
        self.dialog.kind.set(ui.KINDS["text"]); self.dialog.value.set("{trip_date}")
        self.dialog._update()
        self.assertEqual((self.dialog.profile.steps[0].x, self.dialog.profile.steps[0].y), (-1, -1))
        self.dialog._move(1)
        self.assertEqual(self.dialog.profile.window_mode, "recorded")
        self.dialog._remove()
        self.assertEqual(self.dialog.profile.window_mode, "recorded")

    def test_import_draft_is_inert_and_does_not_load_accounts(self):
        draft = self.profile([WorkflowStep("password", "암호 위치", "korail", -1, -1)], geometry=False)
        with patch.object(ui.filedialog, "askopenfilename", return_value="draft.json"), patch.object(ui, "load_workflow", return_value=draft):
            self.dialog._import()
        self.assertIs(self.dialog.profile, draft)
        self.store.get.assert_not_called(); self.engine.play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
