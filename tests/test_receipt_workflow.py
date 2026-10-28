import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from trip_capture.automation import AutomationWorker
from trip_capture.models import CaptureRecord, PreparedPage


class ReceiptWorkflowTests(unittest.TestCase):
    def page(self, url, opener=None):
        page = Mock()
        page.url = url
        page.opener.return_value = opener
        page.is_closed.return_value = False
        return page

    def test_receipt_ownership_survives_blank_blob_and_separate_host(self):
        worker = AutomationWorker(queue.Queue())
        parent = self.page("https://www.hipass.co.kr/usepculr/InitUsePculrTabSearch.do")
        for url in ("about:blank", "blob:https://www.hipass.co.kr/synthetic", "http://127.0.0.1/print", "chrome-extension://test/pdf-viewer"):
            with self.subTest(url=url):
                child = self.page(url, parent)
                self.assertTrue(worker._is_related_page(child, "hipass"))
        unrelated = self.page("http://127.0.0.1/unrelated")
        self.assertFalse(worker._is_related_page(unrelated, "hipass"))

    def test_new_popup_is_tracked_only_during_receipt_step(self):
        worker = AutomationWorker(queue.Queue())
        child = self.page("about:blank")
        worker._configure_page(child)
        self.assertFalse(worker._is_related_page(child, "hipass"))
        worker._current_site = "hipass"
        worker._configure_page(child)
        child.url = "http://127.0.0.1/report-viewer"
        self.assertTrue(worker._is_related_page(child, "hipass"))

    def test_other_site_download_is_not_read(self):
        worker = AutomationWorker(queue.Queue())
        worker._current_site = "hipass"
        download = Mock()
        download.page = self.page("https://unrelated.example/download")
        worker._on_download(download)
        download.save_as.assert_not_called()

    def test_non_pdf_download_is_ignored_without_open_file_deletion_error(self):
        worker = AutomationWorker(queue.Queue())
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "not-a-pdf.pdf"
            file.write_bytes(b"not a PDF")
            worker._queue_pdf_file(file, "https://www.hipass.co.kr/download")
            self.assertFalse(file.exists())
            self.assertTrue(worker.commands.empty())

    def test_invalid_user_pdf_returns_to_waiting_and_preserves_source(self):
        events = queue.Queue()
        worker = AutomationWorker(events)
        worker._context = Mock(pages=[])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.pdf"
            source.write_bytes(b"invalid PDF fixture")
            output = Path(directory) / "output"
            output.mkdir()
            worker.import_pdf(source)
            worker.skip_current()
            self.assertIsNone(worker._manual_capture("hipass", PreparedPage(Mock()), output))
            self.assertEqual(source.read_bytes(), b"invalid PDF fixture")
            self.assertEqual(list(output.iterdir()), [])
            seen = list(events.queue)
            self.assertTrue(any(event["type"] == "manual" and "PDF" in event["message"] for event in seen))
            self.assertFalse(any(event["type"] == "captured" for event in seen))

    def test_pdf_viewer_is_not_recorded_as_a_single_screenshot(self):
        worker = AutomationWorker(queue.Queue())
        page = self.page("https://www.hipass.co.kr/receipt.pdf")
        page.frames = []
        with self.assertRaisesRegex(RuntimeError, "PDF 가져오기"):
            worker._capture("hipass", PreparedPage(page), Path("unused"), manual=False)
        page.screenshot.assert_not_called()

    def test_korail_collects_round_trip_until_explicit_finish_and_saves_partials(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        first = CaptureRecord("korail", "가는편", Path("out.png"), "https://www.korail.com/receipt", "2026-10-23")
        second = CaptureRecord("korail", "오는편", Path("back.png"), "https://www.korail.com/receipt", "2026-10-23")
        worker.capture_current("first")
        worker.capture_current("second")
        worker.finish_receipts()
        partials = []
        with patch.object(worker, "_capture", side_effect=[first, second]):
            records = worker._manual_capture("korail", PreparedPage(Mock()), Path("unused"),
                                             on_saved=lambda saved: partials.append(list(saved)))
        self.assertEqual(records, [first, second])
        self.assertEqual(partials, [[first], [first, second]])

    def test_korail_skip_preserves_receipts_already_collected(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        receipt = CaptureRecord("korail", "영수증", Path("out.png"), "https://www.korail.com/receipt", "2026-10-23")
        worker.capture_current("first")
        worker.commands.put(("cancel", None))
        with patch.object(worker, "_capture", return_value=receipt):
            records = worker._manual_capture("korail", PreparedPage(Mock()), Path("unused"))
        self.assertEqual(records, [receipt])

    def test_korail_popup_does_not_become_a_hipass_page(self):
        worker = AutomationWorker(queue.Queue())
        worker._current_site = "korail"
        page = self.page("about:blank")
        worker._configure_page(page)
        self.assertTrue(worker._is_related_page(page, "korail"))
        self.assertFalse(worker._is_related_page(page, "hipass"))

    def test_late_hipass_popup_is_not_reclassified_by_the_korail_stage(self):
        worker = AutomationWorker(queue.Queue())
        worker._current_site = "korail"
        parent = self.page("https://www.hipass.co.kr/usepculr/receipt")
        child = self.page("about:blank", parent)
        worker._configure_page(child)
        self.assertFalse(worker._is_related_page(child, "korail"))
        self.assertTrue(worker._is_related_page(child, "hipass"))
        unknown = self.page("about:blank")
        worker._configure_page(unknown)
        unknown.url = "https://www.hipass.co.kr/receipt.pdf"
        self.assertFalse(worker._is_related_page(unknown, "korail"))
        self.assertTrue(worker._is_related_page(unknown, "hipass"))

    def test_collected_receipts_survive_browser_context_becoming_unavailable(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock()
        type(worker._context).pages = PropertyMock(side_effect=RuntimeError("Browser closed"))
        record = CaptureRecord("korail", "영수증", Path("out.png"), "https://www.korail.com/receipt", "2026-10-23")
        worker.capture_current("first")
        worker.finish_receipts()
        with patch.object(worker, "_capture", return_value=record):
            result = worker._manual_capture("korail", PreparedPage(Mock()), Path("output"))
        self.assertEqual(result, [record])

    def test_korail_pdf_is_classified_and_waits_for_finish(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        record = CaptureRecord("korail", "PDF", Path("page.png"), "", "2026-10-23")
        worker.import_pdf(Path("train.pdf"))
        worker.finish_receipts()
        with patch("trip_capture.pdf_receipts.import_receipt_pdf", return_value=[record]) as importer:
            records = worker._manual_capture("korail", PreparedPage(Mock()), Path("output"))
        self.assertEqual(records, [record])
        importer.assert_called_once_with(Path("train.pdf"), Path("output"), source_url="", site="korail")

    def test_delayed_download_from_other_site_is_not_attached_to_korail(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        worker.commands.put(("import_pdf", (Path("old.pdf"), "", True, "hipass")))
        worker.finish_receipts()
        with patch("trip_capture.pdf_receipts.import_receipt_pdf") as importer:
            result = worker._manual_capture("korail", PreparedPage(Mock()), Path("output"))
        self.assertIsNone(result)
        importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
