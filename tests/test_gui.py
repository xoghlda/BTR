from __future__ import annotations

import tkinter as tk
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from trip_capture import gui
from trip_capture.models import RunRequest, TRANSPORT_LABELS, TRANSPORT_SITES


class GuiTests(unittest.TestCase):
    """Exercise actual Tk widget states without a browser or user credentials."""

    def setUp(self):
        self.worker = Mock(spec=[
            "start", "submit", "capture_current", "capture_external", "import_pdf", "skip_current", "cancel", "shutdown", "is_alive", "refresh_pages", "finish_receipts", "retry_korail"
        ])
        self.worker.is_alive.return_value = False
        self.credentials = Mock(spec=["get", "set", "delete"])
        self.credentials.get.return_value = None
        self.saved_settings = Mock()
        self.loaded_settings = Mock(return_value={})
        for patcher in (
            patch.object(gui, "AutomationWorker", return_value=self.worker),
            patch.object(gui, "WindowsCredentialStore", return_value=self.credentials),
            patch.object(gui, "load_settings", self.loaded_settings),
            patch.object(gui, "save_settings", self.saved_settings),
            patch.object(gui.messagebox, "showerror"),
            patch.object(gui.messagebox, "showinfo"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = None
        self.addCleanup(self._destroy_root)
        self._make_app()

    def _make_app(self):
        self._destroy_root()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.app = gui.TripCaptureApp(self.root)
        self.root.update_idletasks()

    def _destroy_root(self):
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            self.root = None

    def _start_valid_request(self):
        self.app.origin.set("행정안전부")
        self.app.destination.set("무안국제공항")
        self.app._start()
        self.worker.submit.assert_called_once()
        return self.worker.submit.call_args.args[0]

    def _switch_mode(self, mode):
        self.app.transport_mode.set(mode)
        self.app._mode_changed()

    def _pages(self, selected_id="main", pages=None):
        self.app._handle_event({
            "type": "pages",
            "pages": pages if pages is not None else [
                {"id": "main", "title": "사용내역 조회", "url": "https://hipass.co.kr/use"},
                {"id": "receipt", "title": "하이패스 영수증", "url": "https://hipass.co.kr/print"},
            ],
            "selected_id": selected_id,
        })

    def test_start_locks_inputs_and_disables_manual_capture(self):
        self.assertFalse(self.app.start_button.instate(["disabled"]))
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        request = self._start_valid_request()
        self.assertEqual(request.sites, ("naver", "opinet", "hipass"))
        self.assertFalse(request.use_saved_password)
        self.assertEqual(self.app.run_state, "running")
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.assertTrue(all(control.instate(["disabled"]) for control in self.app.input_controls))

    def test_mode_switch_exposes_only_matching_sites_and_train_station_tab(self):
        self.assertEqual(self.app.transport_mode.get(), "car")
        self.assertFalse(self.app.site_vars["korail"].get())
        self.assertEqual(self.app.details_notebook.tab(self.app.korail_tab, "state"), "hidden")
        self.assertEqual(self.app.hipass_login_frame.winfo_manager(), "grid")
        self.assertEqual(self.app.fuel_combo.winfo_manager(), "grid")
        self._switch_mode("train")
        self.assertEqual(self.app.details_notebook.tab(self.app.korail_tab, "state"), "normal")
        self.assertEqual(self.app.hipass_login_frame.winfo_manager(), "")
        self.assertEqual(self.app.fuel_combo.winfo_manager(), "")
        self.assertEqual([key for key, value in self.app.site_vars.items() if value.get()], ["korail"])
        self.assertTrue(all(control.winfo_manager() == "" for key, control in self.app.site_controls.items() if key != "korail"))
        self.assertEqual(self.app.site_controls["korail"].winfo_manager(), "pack")
        self._switch_mode("car")
        self.assertEqual(self.app.details_notebook.tab(self.app.korail_tab, "state"), "hidden")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), TRANSPORT_SITES["car"])
        self.assertEqual(self.app.hipass_login_frame.winfo_manager(), "grid")

    def test_mode_specific_car_selection_is_remembered_across_switches(self):
        self.app.site_vars["naver"].set(False)
        self.app._sites_changed("naver")
        self._switch_mode("train")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), ("korail",))
        self._switch_mode("car")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), ("opinet", "hipass"))
        self.assertFalse(self.app.site_vars["korail"].get())

    def test_old_mixed_site_settings_migrate_to_train_and_remember_car_subset(self):
        self.loaded_settings.return_value = {"sites": ["opinet", "korail"], "department": "공통 소속"}
        self._make_app()
        self.assertEqual(self.app.transport_mode.get(), "train")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), ("korail",))
        self.assertEqual(self.app.department.get(), "공통 소속")
        self._switch_mode("car")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), ("opinet",))

    def test_explicit_mode_and_each_mode_selection_are_saved_and_restored(self):
        self.app.site_vars["naver"].set(False)
        self.app._sites_changed("naver")
        self._switch_mode("train")
        self.app._save_settings()
        saved = self.saved_settings.call_args.args[0]
        self.assertEqual(saved["transport_mode"], "train")
        self.assertEqual(saved["sites"], ["korail"])
        self.assertEqual(saved["mode_sites"], {"car": ["opinet", "hipass"], "train": ["korail"]})
        self.loaded_settings.return_value = saved
        self._make_app()
        self.assertEqual(self.app.transport_mode.get(), "train")
        self._switch_mode("car")
        self.assertFalse(self.app.site_vars["naver"].get())
        self.assertTrue(self.app.site_vars["opinet"].get())

    def test_mode_cannot_change_while_worker_is_running(self):
        self._start_valid_request()
        self.assertTrue(all(button.instate(["disabled"]) for button in self.app.mode_buttons))
        self._switch_mode("train")
        self.assertEqual(self.app.transport_mode.get(), "car")
        self.assertEqual(self.app._visible_mode, "car")
        self.assertEqual(tuple(key for key, variable in self.app.site_vars.items() if variable.get()), TRANSPORT_SITES["car"])
        self.app._set_run_state("idle")
        self.assertTrue(all(not button.instate(["disabled"]) for button in self.app.mode_buttons))

    def test_hidden_unsaved_hipass_password_does_not_block_or_enter_train_request(self):
        self.app.hipass_id.set("PRIVATE_HIPASS_ID")
        self.app.hipass_password.set("UNSAVED_PRIVATE_PASSWORD")
        self._switch_mode("train")
        self.app._start()
        self.worker.submit.assert_called_once()
        request = self.worker.submit.call_args.args[0]
        self.assertEqual(request.transport_mode, "train")
        self.assertEqual(request.sites, ("korail",))
        self.assertEqual(request.hipass_id, "")
        self.assertFalse(request.use_saved_password)
        self.assertNotIn("UNSAVED_PRIVATE_PASSWORD", repr(request))
        self.assertNotIn("UNSAVED_PRIVATE_PASSWORD", repr(self.saved_settings.call_args))
        self.credentials.get.assert_not_called()
        self.credentials.set.assert_not_called()

    def test_common_trip_metadata_is_editable_and_submitted_for_car(self):
        self.assertEqual(self.app.details_notebook.tab(self.app.common_tab, "state"), "normal")
        self.assertTrue(all(not entry.instate(["disabled"]) for entry in self.app.common_entries.values()))
        self.app.department.set("공통 소속")
        self.app.position.set("공통 직급")
        self.app.traveler_name.set("가상인물")
        self.app.trip_purpose.set("공통 출장 목적")
        request = self._start_valid_request()
        self.assertEqual(request.transport_mode, "car")
        self.assertEqual(request.department, "공통 소속")
        self.assertEqual(request.position, "공통 직급")
        self.assertEqual(request.traveler_name, "가상인물")
        self.assertEqual(request.trip_purpose, "공통 출장 목적")

    def test_gui_cannot_submit_hidden_sites_from_other_mode(self):
        self.app.site_vars["korail"].set(True)
        request = self._start_valid_request()
        self.assertEqual(request.transport_mode, "car")
        self.assertEqual(request.sites, TRANSPORT_SITES["car"])

    def test_korail_metadata_is_in_request_and_settings_without_korail_credentials(self):
        self._switch_mode("train")
        self.app.trip_date.set("2026-09-28")
        metadata = {"trip_end_date": "2026-09-30", "department": "행정안전부", "position": "사무관", "traveler_name": "홍길동", "trip_purpose": "업무 협의", "rail_origin": "오송", "rail_destination": "서울", "rail_return_origin": "용산", "rail_return_destination": "오송"}
        for key, value in metadata.items():
            self.app.trip_details[key].set(value)
        self.app._start()
        request = self.worker.submit.call_args.args[0]
        self.assertEqual(request.sites, ("korail",))
        self.assertEqual(request.transport_mode, "train")
        for key, value in metadata.items():
            self.assertEqual(getattr(request, key), value)
            self.assertEqual(self.saved_settings.call_args.args[0][key], value)
        self.assertFalse(request.use_saved_password)
        self.assertFalse(hasattr(request, "korail_password"))

    def test_return_station_preferences_restore_and_blank_defaults_stay_blank(self):
        self.assertEqual(self.app.rail_return_origin.get(), "")
        self.assertEqual(self.app.rail_return_destination.get(), "")
        self.loaded_settings.return_value = {"sites": ["korail"], "rail_origin": "오송", "rail_destination": "서울", "rail_return_origin": "용산", "rail_return_destination": "오송"}
        self._make_app()
        self.assertEqual(self.app.rail_return_origin.get(), "용산")
        self.assertEqual(self.app.rail_return_destination.get(), "오송")
        self.app._save_settings()
        self.assertEqual(self.saved_settings.call_args.args[0]["rail_return_origin"], "용산")
        self.assertEqual(self.saved_settings.call_args.args[0]["rail_return_destination"], "오송")

    def test_korail_receipt_actions_finish_and_retry_only_enabled_while_waiting(self):
        self.assertTrue(self.app.finish_button.instate(["disabled"]))
        self.assertTrue(self.app.korail_retry_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증"})
        self.assertTrue(self.app.finish_button.instate(["disabled"]))
        self.app._set_run_state("idle")
        self._switch_mode("train")
        self.app._handle_event({"type": "manual", "site": "korail", "message": "직접 로그인"})
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))
        self.assertFalse(self.app.external_button.instate(["disabled"]))
        self.assertFalse(self.app.finish_button.instate(["disabled"]))
        self.assertFalse(self.app.korail_retry_button.instate(["disabled"]))
        self.app.korail_retry_button.invoke()
        self.worker.retry_korail.assert_called_once()
        self.assertEqual(self.app.run_state, "running")
        self.app._handle_event({"type": "manual", "site": "korail", "message": "다음 영수증"})
        self.app.finish_button.invoke()
        self.worker.finish_receipts.assert_called_once()
        self.assertTrue(self.app.finish_button.instate(["disabled"]))
        self.assertTrue(self.app.pdf_button.instate(["disabled"]))

    def test_korail_pdf_and_native_dialog_keep_korail_site(self):
        self._switch_mode("train")
        self.app._handle_event({"type": "manual", "site": "korail", "message": "영수증 선택"})
        with patch.object(gui, "NativeCaptureDialog") as dialog:
            self.app.external_button.invoke()
        dialog.assert_called_once_with(self.root, self.app._external_captured, site="korail")
        with patch.object(gui.filedialog, "askopenfilename", return_value="C:/selected/ktx.pdf"):
            self.app.pdf_button.invoke()
        self.worker.import_pdf.assert_called_once_with(Path("C:/selected/ktx.pdf"))

    def test_official_korail_button_opens_public_ticket_site(self):
        with patch.object(gui.webbrowser, "open") as browser_open:
            self.app._open_korail()
        browser_open.assert_called_once_with("https://www.korail.com/ticket")

    def test_browserless_train_uses_regular_browser_and_keeps_imports_available(self):
        self._switch_mode("train")
        self.app._start()
        manual = {"type": "manual", "site": "korail", "browserless": True,
                  "collected_count": 0, "message": "평소 브라우저에서 영수증을 저장하세요."}
        with patch.object(gui.webbrowser, "open", return_value=True) as browser_open:
            self.app._handle_event(manual)
            browser_open.assert_called_once_with("https://www.korail.com/ticket")
            self.assertEqual(self.app.run_state, "waiting")
            self.assertFalse(self.app.pdf_button.instate(["disabled"]))
            self.assertFalse(self.app.external_button.instate(["disabled"]))
            self.assertTrue(self.app.finish_button.instate(["disabled"]))
            self.assertTrue(self.app.capture_button.instate(["disabled"]))
            self.assertTrue(self.app.refresh_button.instate(["disabled"]))
            self.assertEqual(self.app.page_controls.winfo_manager(), "")
            self.assertEqual(self.app.capture_button.winfo_manager(), "")
            self.assertEqual(self.app.skip_button.winfo_manager(), "")
            self.worker.refresh_pages.assert_not_called()
            self.app.korail_retry_button.invoke()
            self.assertEqual(browser_open.call_count, 2)
            self.assertEqual(self.app.run_state, "waiting")
            self.worker.retry_korail.assert_not_called()
            self.app._handle_event({**manual, "message": "파일 오류: 다시 선택하세요."})
            self.assertEqual(browser_open.call_count, 2)
            self.assertFalse(self.app.pdf_button.instate(["disabled"]))
            self.app._handle_event({**manual, "collected_count": 2, "message": "2개 페이지 저장"})
            self.assertFalse(self.app.finish_button.instate(["disabled"]))
        self.app.finish_button.invoke()
        self.worker.finish_receipts.assert_called_once()

    def test_regular_browser_open_failure_preserves_train_pdf_and_window_actions(self):
        self._switch_mode("train")
        with patch.object(gui.webbrowser, "open", side_effect=OSError("launch failed")):
            self.app._handle_event({"type": "manual", "site": "korail", "browserless": True,
                                    "message": "PDF 또는 출력창을 추가하세요.", "collected_count": 0})
        self.assertEqual(self.app.run_state, "waiting")
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))
        self.assertFalse(self.app.external_button.instate(["disabled"]))
        self.assertIn("기본 브라우저를 열지 못했습니다", self.app.log_widget.get("1.0", "end"))
        with patch.object(gui.filedialog, "askopenfilename", return_value=""):
            self.app.pdf_button.invoke()
        self.assertEqual(self.app.run_state, "waiting")
        self.worker.import_pdf.assert_not_called()

    def test_old_saved_end_date_is_not_reused_with_new_today_start(self):
        self.loaded_settings.return_value = {"trip_date": "2026-09-28", "trip_end_date": "2026-09-30", "sites": ["korail"], "department": "소속"}
        self._make_app()
        self.assertEqual(self.app.trip_date.get(), date.today().isoformat())
        self.assertEqual(self.app.trip_end_date.get(), "")
        self.assertEqual(self.app.department.get(), "소속")
        self.assertEqual(self.app.details_notebook.tab(self.app.korail_tab, "state"), "normal")

    def test_normal_browser_capture_is_enabled_only_during_hipass_handoff(self):
        self.assertTrue(self.app.external_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "naver", "message": "장소 확인"})
        self.assertTrue(self.app.external_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 준비"})
        self.assertFalse(self.app.external_button.instate(["disabled"]))
        image = Path("staged-test.png")
        self.app._external_captured(image, "하이패스 영수증")
        self.worker.capture_external.assert_called_once_with(image, "하이패스 영수증")
        self.assertEqual(self.app.run_state, "running")
        self.assertTrue(self.app.external_button.instate(["disabled"]))
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        self.assertTrue(self.app.skip_button.instate(["disabled"]))
        self.assertFalse(self.app.cancel_button.instate(["disabled"]))

    def test_pdf_import_requires_hipass_waiting_and_forwards_only_user_selected_path(self):
        self.assertTrue(self.app.pdf_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "opinet", "message": "유가 준비"})
        self.assertTrue(self.app.pdf_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 준비"})
        self._pages(selected_id="receipt")
        selected_label = self.app.page_selection.get()
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))
        with patch.object(gui.filedialog, "askopenfilename", return_value="C:/selected/receipt.pdf") as choose:
            self.app.pdf_button.invoke()
        self.worker.import_pdf.assert_called_once_with(Path("C:/selected/receipt.pdf"))
        self.assertEqual(choose.call_args.kwargs["filetypes"], [("PDF 영수증", "*.pdf")])
        self.assertEqual(self.app.page_selection.get(), selected_label)
        self.assertEqual(self.app.run_state, "running")
        self.assertTrue(self.app.pdf_button.instate(["disabled"]))
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "PDF 오류: 다시 선택"})
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))
        self.assertEqual(self.app.page_selection.get(), selected_label)

    def test_cancel_pdf_file_picker_does_not_change_waiting_state(self):
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 준비"})
        with patch.object(gui.filedialog, "askopenfilename", return_value=""):
            self.app.pdf_button.invoke()
        self.worker.import_pdf.assert_not_called()
        self.assertEqual(self.app.run_state, "waiting")
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))

    def test_multipage_pdf_count_does_not_hide_missing_sites(self):
        self._start_valid_request()
        self.app._handle_event({"type": "done", "count": 6, "site_count": 1})
        self.assertIn("선택한 3개 중 1개 사이트", self.app.status.get())
        self.assertIn("화면·페이지 6개", self.app.status.get())
        self.assertNotIn("작업 완료", self.app.status.get())

    def test_manual_capture_requires_selected_window_and_recovers_after_failure(self):
        self._start_valid_request()
        manual = {"type": "manual", "site": "hipass", "message": "영수증 화면을 준비하세요."}
        self.app._handle_event(manual)
        self.worker.refresh_pages.assert_called_once()
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        self.assertFalse(self.app.skip_button.instate(["disabled"]))
        self.assertFalse(self.app.refresh_button.instate(["disabled"]))
        self._pages()
        receipt_label = next(label for label, page_id in self.app._page_ids.items() if page_id == "receipt")
        self.app.page_selection.set(receipt_label)
        self.app.page_combo.event_generate("<<ComboboxSelected>>")
        self.root.update_idletasks()
        self.assertFalse(self.app.capture_button.instate(["disabled"]))
        self.app.capture_button.invoke()
        self.worker.capture_current.assert_called_once_with("receipt")
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        self.assertTrue(self.app.skip_button.instate(["disabled"]))
        self.assertTrue(self.app.page_combo.instate(["disabled"]))
        self.app._handle_event({**manual, "message": "저장 실패: 화면을 확인하고 다시 저장하세요."})
        self._pages(selected_id="main")
        self.assertEqual(self.app._page_ids[self.app.page_selection.get()], "receipt")
        self.assertFalse(self.app.capture_button.instate(["disabled"]))
        self.assertFalse(self.app.skip_button.instate(["disabled"]))
        self.assertIn("저장 실패", self.app.manual_instruction.get())

    def test_window_refresh_preserves_selection_and_falls_back_when_closed(self):
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "화면 선택"})
        self._pages(selected_id="receipt")
        self._pages(selected_id="main")
        self.assertEqual(self.app._page_ids[self.app.page_selection.get()], "receipt")
        self._pages(selected_id="main", pages=[{"id": "main", "title": "사용내역", "url": "https://hipass.co.kr"}])
        self.assertEqual(self.app._page_ids[self.app.page_selection.get()], "main")
        self._pages(selected_id="", pages=[])
        self.assertEqual(self.app.page_selection.get(), "")
        self.assertTrue(self.app.capture_button.instate(["disabled"]))

    def test_next_site_needs_its_own_window_selection(self):
        self.app._handle_event({"type": "manual", "site": "opinet", "message": "유가 조회"})
        self._pages(selected_id="opinet", pages=[{"id": "opinet", "title": "유가", "url": "https://opinet.co.kr"}])
        self.assertFalse(self.app.capture_button.instate(["disabled"]))
        self.app._handle_event({"type": "status", "site": "hipass", "message": "준비 중"})
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 조회"})
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        self.assertFalse(self.app._page_ids)

    def test_zero_capture_is_not_reported_as_success(self):
        self._start_valid_request()
        self.app._handle_event({
            "type": "done", "count": 0,
            "report_path": str(Path("C:/results/report.html")), "output_dir": str(Path("C:/results")),
        })
        self.assertEqual(self.app.status.get(), "저장된 화면 없음 · 확인 필요")
        self.assertNotIn("작업 완료", self.app.status.get())
        self.assertIn("저장되지 않았습니다", self.app.manual_instruction.get())
        self.assertEqual(self.app.run_state, "idle")
        self.assertFalse(self.app.start_button.instate(["disabled"]))
        self.assertTrue(all(not control.instate(["disabled"]) for control in self.app.input_controls))
        self.assertFalse(self.app.folder_button.instate(["disabled"]))
        self.assertFalse(self.app.report_button.instate(["disabled"]))
        self.assertTrue(self.app.capture_button.instate(["disabled"]))

    def test_partial_result_reports_missing_site_count(self):
        self._start_valid_request()
        self.app._handle_event({"type": "done", "count": 1})
        self.assertIn("선택한 3개 중 1개", self.app.status.get())
        self.assertIn("누락", self.app.manual_instruction.get())

    def test_step_shows_current_and_next_site_without_extra_rows(self):
        self.assertIn("v1.4.1", self.root.title())
        self.app._handle_event({"type": "step", "site": "naver", "index": 1, "total": 3, "next_site": "opinet", "sites": ["naver", "opinet", "hipass"]})
        self.app._handle_event({"type": "manual", "site": "naver", "message": "경로 준비"})
        title = self.app.progress_frame.cget("text")
        self.assertIn("1/3", title)
        self.assertIn("다음: 오피넷", title)
        self.assertNotIn("코레일 미선택", title)
        self.assertEqual(self.app.skip_button.cget("text"), "이 사이트 건너뛰기")
        self.app.skip_button.invoke()
        self.worker.skip_current.assert_called_once()
        self.assertIn("오피넷 일일 유가 준비 중", self.app.status.get())
        self.assertIn("다음 항목", self.app.manual_instruction.get())
        self.worker.shutdown.assert_not_called()

    def test_selection_header_names_current_mode_without_unselected_type_warning(self):
        self.assertIn("자차 이용", self.app.progress_frame.cget("text"))
        self.assertNotIn("코레일 미선택", self.app.progress_frame.cget("text"))
        self._switch_mode("train")
        self.assertIn("기차 이용", self.app.progress_frame.cget("text"))
        self.assertTrue(self.app.progress_frame.cget("text").endswith("코레일"))
        self.assertEqual(self.app.details_notebook.tab(self.app.korail_tab, "state"), "normal")

    def test_last_skip_explicitly_says_report_will_be_prepared(self):
        self.app._handle_event({"type": "step", "site": "hipass", "index": 3, "total": 3, "next_site": "", "sites": ["naver", "opinet", "hipass"]})
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 준비"})
        self.assertIn("3/3", self.app.progress_frame.cget("text"))
        self.assertIn("마지막 항목", self.app.progress_frame.cget("text"))
        self.assertIn("자차 이용", self.app.progress_frame.cget("text"))
        self.assertEqual(self.app.skip_button.cget("text"), "마지막 항목 건너뛰기")
        self.app.skip_button.invoke()
        self.worker.skip_current.assert_called_once()
        self.assertIn("마지막 항목을 건너뛰고 보고서", self.app.status.get())
        self.assertEqual(self.app.run_state, "running")
        self.assertFalse(self.app._closed)
        self.worker.shutdown.assert_not_called()

    def test_done_lists_skipped_sites_and_keeps_current_transport_label(self):
        self._start_valid_request()
        self.app._handle_event({"type": "done", "count": 2, "site_count": 2, "skipped_sites": ["hipass"], "report_path": "C:/results/report.html", "output_dir": "C:/results"})
        self.assertIn("건너뜀 1개", self.app.status.get())
        self.assertIn("건너뛴 항목: 하이패스 영수증", self.app.manual_instruction.get())
        self.assertNotIn("코레일은 선택하지 않았습니다", self.app.manual_instruction.get())
        self.assertIn("자차 이용", self.app.progress_frame.cget("text"))
        self.assertFalse(self.app.start_button.instate(["disabled"]))
        self.assertFalse(self.app.report_button.instate(["disabled"]))
        self.assertFalse(self.app._closed)
        self.worker.shutdown.assert_not_called()

    def test_legacy_manual_event_infers_next_or_last_from_selected_order(self):
        self.app._handle_event({"type": "manual", "site": "opinet", "message": "유가 준비"})
        self.assertIn("2/3", self.app.progress_frame.cget("text"))
        self.assertIn("다음: 하이패스", self.app.progress_frame.cget("text"))
        self.app._handle_event({"type": "manual", "site": "hipass", "message": "영수증 준비"})
        self.assertEqual(self.app.skip_button.cget("text"), "마지막 항목 건너뛰기")

    def test_site_preferences_restore_and_password_is_never_in_settings(self):
        self.loaded_settings.return_value = {"sites": ["opinet"], "hipass_id": "sample"}
        self._make_app()
        self.assertFalse(self.app.site_vars["naver"].get())
        self.assertTrue(self.app.site_vars["opinet"].get())
        self.assertFalse(self.app.site_vars["hipass"].get())
        self.app.hipass_password.set("PRIVATE_PASSWORD")
        self.app._save_settings()
        settings = self.saved_settings.call_args.args[0]
        self.assertEqual(settings["sites"], ["opinet"])
        self.assertNotIn("password", settings)
        self.assertNotIn("PRIVATE_PASSWORD", repr(settings))

    def test_password_requires_explicit_save_before_run(self):
        self.app.hipass_id.set("sample")
        self.app.hipass_password.set("PRIVATE_PASSWORD")
        self.app._start()
        self.worker.submit.assert_not_called()
        self.credentials.set.assert_not_called()
        self.app._save_credentials()
        self.credentials.set.assert_called_once_with("hipass:sample", "PRIVATE_PASSWORD")
        self.assertEqual(self.app.hipass_password.get(), "")
        self.credentials.get.return_value = "PRIVATE_PASSWORD"
        request = self._start_valid_request()
        self.assertTrue(request.use_saved_password)
        self.assertNotIn("PRIVATE_PASSWORD", repr(request))
        self.assertNotIn("PRIVATE_PASSWORD", repr(self.saved_settings.call_args))

    def test_visible_errors_and_logs_redact_current_password(self):
        self.app.hipass_password.set("PRIVATE_PASSWORD")
        self.app._handle_event({"type": "error", "message": "입력값 PRIVATE_PASSWORD 처리 실패"})
        self.assertNotIn("PRIVATE_PASSWORD", self.app.status.get())
        self.assertNotIn("PRIVATE_PASSWORD", self.app.log_widget.get("1.0", "end"))

    def test_macro_manager_is_singleton_and_blocks_competing_capture_actions(self):
        self.app._handle_event({"type": "manual", "site": "korail", "message": "영수증 준비"})
        self.app.last_report = Path("C:/development/report.html")
        self.app.last_output = Path("C:/development")
        self.app._set_run_state("waiting")
        dialog = Mock()
        dialog.is_busy.return_value = False
        with patch.object(gui, "BrowserMacroDialog", return_value=dialog) as factory:
            self.app.macro_button.invoke()
            self.app._open_browser_macro()
        factory.assert_called_once_with(self.root, self.app._macro_busy_changed, self.app._macro_closed)
        dialog.lift.assert_called_once()
        self.assertTrue(self.app.pdf_button.instate(["disabled"]))
        self.assertTrue(self.app.external_button.instate(["disabled"]))
        self.assertTrue(self.app.finish_button.instate(["disabled"]))
        self.assertTrue(self.app.report_button.instate(["disabled"]))
        self.assertTrue(self.app.folder_button.instate(["disabled"]))
        self.worker.refresh_pages.reset_mock()
        self.app._capture()
        self.app._refresh_pages()
        self.app._finish_receipts()
        self.app._skip()
        self.worker.capture_current.assert_not_called()
        self.worker.refresh_pages.assert_not_called()
        self.worker.finish_receipts.assert_not_called()
        self.worker.skip_current.assert_not_called()
        self.app._macro_busy_changed(True)
        self.assertTrue(self.app.macro_button.instate(["disabled"]))
        self.app._macro_closed()
        self.assertEqual(self.app.run_state, "waiting")
        self.assertFalse(self.app.pdf_button.instate(["disabled"]))
        self.assertFalse(self.app.external_button.instate(["disabled"]))
        self.assertFalse(self.app.report_button.instate(["disabled"]))
        self.assertFalse(self.app.folder_button.instate(["disabled"]))

    def test_macro_manager_blocks_run_and_refuses_running_worker_or_native_capture(self):
        dialog = Mock()
        with patch.object(gui, "BrowserMacroDialog", return_value=dialog) as factory:
            self.app._set_run_state("running")
            self.app._open_browser_macro()
            factory.assert_not_called()
            self.app._set_run_state("idle")
            self.app._native_dialog = Mock()
            self.app._native_dialog.winfo_exists.return_value = True
            self.app._open_browser_macro()
            factory.assert_not_called()
            self.app._native_dialog = None
            self.app._open_browser_macro()
            self.app._start()
        self.worker.submit.assert_not_called()
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.app._macro_closed()
        self.assertFalse(self.app.start_button.instate(["disabled"]))

    def test_close_waits_for_macro_stop_before_destroying_root(self):
        dialog = Mock()
        dialog.is_busy.return_value = True
        self.app._macro_dialog = dialog
        with patch.object(self.root, "after") as after:
            self.app._close()
        dialog.close.assert_called_once()
        self.worker.shutdown.assert_called_once()
        after.assert_called_once_with(100, self.app._wait_for_worker_close)
        self.assertEqual(self.root.winfo_exists(), 1)
        dialog.is_busy.return_value = False
        self.app._wait_for_worker_close()
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()

    def test_close_waits_for_worker_without_blocking_tk(self):
        self.worker.is_alive.return_value = True
        with patch.object(gui.time, "monotonic", return_value=10.0), patch.object(self.root, "after") as after:
            self.app._close()
        self.worker.shutdown.assert_called_once()
        after.assert_called_once_with(100, self.app._wait_for_worker_close)
        self.assertEqual(self.root.winfo_exists(), 1)
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.assertTrue(self.app.cancel_button.instate(["disabled"]))
        self.worker.is_alive.return_value = False
        self.app._wait_for_worker_close()
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()

    def test_close_keeps_waiting_for_browser_cleanup_after_eight_seconds(self):
        self.worker.is_alive.return_value = True
        with patch.object(gui.time, "monotonic", side_effect=[10.0, 10.0]), patch.object(self.root, "after"):
            self.app._close()
        with patch.object(gui.time, "monotonic", return_value=18.01), patch.object(self.root, "after") as after:
            self.app._wait_for_worker_close()
        self.assertEqual(self.root.winfo_exists(), 1)
        self.assertIn("브라우저 응답을 기다리는 중", self.app.status.get())
        after.assert_called_once_with(100, self.app._wait_for_worker_close)
        self.worker.is_alive.return_value = False
        self.app._wait_for_worker_close()


class TripPeriodValidationTests(unittest.TestCase):
    def test_explicit_transport_modes_reject_mixed_or_unknown_types(self):
        for mode, sites in (("car", ("korail",)), ("train", ("naver",)), ("train", ("korail", "hipass")), ("other", ("korail",))):
            with self.subTest(mode=mode, sites=sites), self.assertRaises(ValueError):
                RunRequest("2026-09-28", "세종", "서울", sites=sites, transport_mode=mode).validate()
        for mode, sites in TRANSPORT_SITES.items():
            request = RunRequest("2026-09-28", "세종", "서울", sites=sites, transport_mode=mode)
            request.validate()
            self.assertEqual(request.resolved_transport_mode, mode)

    def test_legacy_mode_resolution_preserves_old_constructor_and_mixed_sites(self):
        self.assertEqual(RunRequest("2026-09-28", "세종", "서울").resolved_transport_mode, "car")
        legacy = RunRequest("2026-09-28", "세종", "서울", sites=("naver", "korail"))
        legacy.validate()
        self.assertEqual(legacy.transport_mode, "")
        self.assertEqual(legacy.resolved_transport_mode, "train")
        self.assertEqual(TRANSPORT_LABELS, {"car": "자차 이용", "train": "기차 이용"})

    def test_original_positional_constructor_and_default_sites_are_preserved(self):
        request = RunRequest("2026-09-28", "세종", "목포", "diesel", ("naver",))
        request.validate()
        self.assertEqual(request.trip_end_date, "")
        self.assertEqual(request.rail_return_origin, "")
        self.assertEqual(request.rail_return_destination, "")
        self.assertEqual(RunRequest("2026-09-28", "", "").sites, ("naver", "opinet", "hipass"))

    def test_end_date_before_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "시작일보다"):
            RunRequest("2026-09-30", "", "", sites=("korail",), trip_end_date="2026-09-29").validate()

    def test_future_end_is_rejected_and_blank_means_same_day(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "종료일은 오늘"):
            RunRequest("2026-09-28", "", "", sites=("korail",), trip_end_date=tomorrow).validate()
        RunRequest("2026-09-28", "", "", sites=("korail",)).validate()


if __name__ == "__main__":
    unittest.main()
