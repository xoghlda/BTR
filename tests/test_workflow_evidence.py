from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from trip_capture.models import RunRequest
from trip_capture.native_capture import NativeBrowserWindow
from trip_capture.workflow_evidence import WorkflowEvidence


class WorkflowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.request = RunRequest(
            trip_date=date.today().isoformat(), origin="개발용 출발지", destination="개발용 출장지",
            sites=("korail",), transport_mode="train", output_dir=self.root,
            hipass_id="DO_NOT_WRITE_ACCOUNT", use_saved_password=True,
        )
        self.evidence = WorkflowEvidence(self.request)
        self.window = NativeBrowserWindow(123, "가상 연습 화면", "Edge")

    @staticmethod
    def _fake_capture(_window, path):
        with Image.new("RGB", (40, 30), "white") as image:
            image.save(path)

    def test_capture_preserves_screenshot_and_report_provenance(self):
        with patch("trip_capture.workflow_evidence.save_visible_browser_window", side_effect=self._fake_capture):
            self.evidence.capture(self.window, "korail", "영수증", "https://www.korail.com/receipt?token=PRIVATE")
        record = self.evidence.records[0]
        before = hashlib.sha256(record.image_path.read_bytes()).hexdigest()
        report = self.evidence.finish("complete")
        self.assertTrue(report.is_file())
        self.assertTrue((report.parent / "rail_settlement.html").is_file())
        self.assertFalse((report.parent / "car_settlement.html").exists())
        self.assertEqual(before, hashlib.sha256(record.image_path.read_bytes()).hexdigest())
        manifest = (report.parent / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", manifest)
        self.assertNotIn("DO_NOT_WRITE_ACCOUNT", manifest)
        self.assertIn("일반 브라우저 전체 흐름", manifest)

    def test_partial_report_keeps_saved_material_and_missing_site(self):
        self.evidence = WorkflowEvidence(RunRequest(
            trip_date=date.today().isoformat(), origin="가상 출발", destination="가상 도착",
            sites=("naver", "opinet", "hipass"), transport_mode="car", output_dir=self.root,
        ))
        with patch("trip_capture.workflow_evidence.save_visible_browser_window", side_effect=self._fake_capture):
            self.evidence.capture(self.window, "naver", "개발용 경로 화면", "https://map.naver.com/")
        report = self.evidence.finish("cancelled")
        content = report.read_text(encoding="utf-8")
        self.assertIn("자동 흐름이 중간에 멈췄습니다", content)
        self.assertTrue((report.parent / "car_settlement.html").is_file())
        self.assertEqual(self.evidence.finish("complete"), report)
        self.assertEqual(len(self.evidence.records), 1)

    def test_pdf_all_pages_and_original_preserved(self):
        source = Path(__file__).resolve().parents[1] / "releases/v1.3.1/windows-x64/samples/DEVELOPMENT_ONLY_SYNTHETIC_3PAGES.pdf"
        before = source.read_bytes()
        self.evidence.import_pdf(source, "korail", "https://www.korail.com/receipt")
        self.assertEqual(len(self.evidence.records), 3)
        self.assertEqual(self.evidence.records[0].original_path.read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(self.evidence.finish("complete").exists())

    def test_wrong_mode_or_unselected_site_never_captures(self):
        with patch("trip_capture.workflow_evidence.save_visible_browser_window") as capture:
            with self.assertRaises(ValueError):
                self.evidence.capture(self.window, "hipass", "오류", "")
        capture.assert_not_called()
        self.assertIsNone(self.evidence.output_dir)

    def test_failed_capture_never_leaves_partial_image_or_record(self):
        def fail(_window, path):
            path.write_bytes(b"partial")
            raise RuntimeError("test capture failure")
        with patch("trip_capture.workflow_evidence.save_visible_browser_window", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.evidence.capture(self.window, "korail", "개발용 실패", "")
        self.assertFalse(self.evidence.records)
        self.assertFalse(list(self.evidence.output_dir.glob("*.png")))

    def test_finish_without_material_shows_missing_evidence(self):
        report = self.evidence.finish("failed")
        data = json.loads((report.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(data.get("status"), "complete")
        with self.assertRaises(ValueError):
            self.evidence.import_pdf(Path("not-read.pdf"), "korail", "")
