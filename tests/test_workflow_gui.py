from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from trip_capture import gui


class WorkflowGuiTests(unittest.TestCase):
    def setUp(self):
        self.worker = Mock()
        self.worker.is_alive.return_value = False
        self.credentials = Mock()
        self.credentials.get.return_value = None
        self.dialog = Mock()
        for patcher in (
            patch.object(gui, "AutomationWorker", return_value=self.worker),
            patch.object(gui, "WindowsCredentialStore", return_value=self.credentials),
            patch.object(gui, "load_settings", return_value={}),
            patch.object(gui, "save_settings"),
            patch.object(gui, "WorkflowDialog", return_value=self.dialog),
            patch.object(gui.messagebox, "showerror"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = gui.TripCaptureApp(self.root)
        self.app.origin.set("개발용 출발지")
        self.app.destination.set("개발용 도착지")

    def test_full_workflow_uses_trip_fields_and_locks_other_operations(self):
        self.app._open_workflow()
        args = gui.WorkflowDialog.call_args
        request = args.kwargs["request"]
        self.assertEqual(request.browser, "msedge")
        self.assertEqual(request.sites, ("naver", "opinet", "hipass"))
        self.assertEqual(request.origin, "개발용 출발지")
        self.assertEqual(request.hipass_id, "")
        self.assertTrue(self.app.workflow_button.instate(["disabled"]))
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.app._start()
        self.worker.submit.assert_not_called()

    def test_train_workflow_restricts_sites_and_preserves_stations(self):
        self.app.transport_mode.set("train")
        self.app._mode_changed()
        self.app.rail_origin.set("오송")
        self.app.rail_destination.set("서울")
        self.app._open_workflow()
        request = gui.WorkflowDialog.call_args.kwargs["request"]
        self.assertEqual(request.sites, ("korail",))
        self.assertEqual(request.rail_origin, "오송")
        self.assertEqual(request.resolved_transport_mode, "train")

    def test_running_or_waiting_collection_cannot_start_independent_workflow(self):
        for state in ("running", "waiting"):
            self.app._set_run_state(state)
            self.app._open_workflow()
            self.assertTrue(self.app.workflow_button.instate(["disabled"]))
        gui.WorkflowDialog.assert_not_called()

    def test_invalid_date_cannot_open_workflow(self):
        self.app.trip_date.set("invalid")
        self.app._open_workflow()
        gui.WorkflowDialog.assert_not_called()
        gui.messagebox.showerror.assert_called_once()

    def test_report_available_only_after_dialog_closes(self):
        self.app._open_workflow()
        report = Path("C:/development-only/travel_report.html")
        self.app._workflow_report(report)
        self.assertEqual(self.app.last_output, report.parent)
        self.assertTrue(self.app.report_button.instate(["disabled"]))
        self.app._macro_closed()
        self.assertFalse(self.app.report_button.instate(["disabled"]))
        self.assertFalse(self.app.workflow_button.instate(["disabled"]))

    def test_close_cancels_workflow_and_waits_for_worker(self):
        self.app._open_workflow()
        self.dialog.is_busy.return_value = True
        with patch.object(self.root, "destroy") as destroy:
            self.app._close()
            self.dialog.close.assert_called_once()
            destroy.assert_not_called()
            self.dialog.is_busy.return_value = False
            self.app._wait_for_worker_close()
            destroy.assert_called_once()
