"""Exercise ordinary-browser receipt collection without any browser/account."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

from PIL import Image, ImageDraw

from trip_capture.automation import AutomationWorker
from trip_capture.models import PreparedPage, RunRequest


def synthetic_pdf(page_count: int = 3) -> bytes:
    """Small valid fixture with a conspicuous synthetic-data label on every page."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Count {page_count} /Kids [" + " ".join(f"{4 + index * 2} 0 R" for index in range(page_count)) + "] >>").encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(page_count):
        page_id = 4 + index * 2
        stream = f"BT /F1 16 Tf 20 360 Td (SYNTHETIC DEVELOPMENT DATA - {index + 1}) Tj ET\n".encode("ascii")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 420] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode("ascii"),
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
        ])
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode("ascii") + payload + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return output.getvalue()


class BrowserlessKorailTests(unittest.TestCase):
    def request(self, root: Path) -> RunRequest:
        return RunRequest(
            trip_date="2026-09-30", trip_end_date="2026-10-01", origin="개발용 세종", destination="개발용 서울",
            sites=("korail",), transport_mode="train", output_dir=root / "reports",
            rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송",
            # A saved car setting must not read credentials during a train-only run.
            hipass_id="DEVELOPMENT_UNUSED_ID", use_saved_password=True,
        )

    def run_request(self, root: Path, commands, *, events=None):
        events = events or queue.Queue()
        worker = AutomationWorker(events)
        stale_context = Mock()
        pages = PropertyMock(side_effect=AssertionError("Do not inspect an old managed context"))
        type(stale_context).pages = pages
        worker._context = stale_context
        for command in commands:
            worker.commands.put(command)
        with patch.object(worker, "_ensure_browser", side_effect=AssertionError("No Playwright launch is allowed")) as launch, \
             patch("trip_capture.korail.prepare_korail", side_effect=AssertionError("Do not open a managed Korail page")) as managed, \
             patch("trip_capture.automation.WindowsCredentialStore", side_effect=AssertionError("No credentials are needed")) as credentials, \
             patch("trip_capture.automation.app_data_dir", return_value=root / "app"):
            worker._run_request(self.request(root))
        launch.assert_not_called()
        managed.assert_not_called()
        credentials.assert_not_called()
        pages.assert_not_called()
        seen = list(events.queue)
        done = [event for event in seen if event["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual([event["value"] for event in seen if event["type"] == "state"][-1], "idle")
        manual = [event for event in seen if event["type"] == "manual"]
        self.assertTrue(manual)
        self.assertTrue(all(event["browserless"] for event in manual))
        self.assertTrue(all(event["pages"] == [] and event["selected_id"] == "" for event in seen if event["type"] == "pages"))
        output = Path(done[0]["output_dir"])
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(any("처리 중 오류" in notice for notice in manifest["notices"]))
        return worker, seen, done[0], output, manifest

    def test_empty_finish_keeps_waiting_then_pdf_preserves_all_original_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "development-synthetic-three-pages.pdf"
            original = synthetic_pdf()
            source.write_bytes(original)
            worker, seen, done, output, manifest = self.run_request(root, (
                ("finish_receipts", None),
                ("import_pdf", (source, "", False)),
                ("finish_receipts", None),
            ))
            self.assertEqual(done["count"], 3)
            self.assertEqual(done["site_count"], 1)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["trip"]["transport_mode"], "train")
            self.assertEqual(manifest["trip"]["rail_origin"], "오송")
            self.assertEqual(manifest["trip"]["rail_destination"], "서울")
            self.assertEqual(manifest["trip"]["rail_return_origin"], "용산")
            self.assertEqual(manifest["trip"]["rail_return_destination"], "오송")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(manifest["attachments"]), 1)
            attachment = manifest["attachments"][0]
            self.assertEqual(attachment["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual((output / attachment["path"]).read_bytes(), original)
            self.assertEqual(len(list(output.glob("korail*_page_*.png"))), 3)
            self.assertTrue(all(entry["site"] == "korail" and entry["manual"] for entry in manifest["captures"]))
            # User-selected originals are preserved; no login or travel-field
            # verification is invented from the requested stations.
            self.assertTrue(all("승차일" not in entry["details"] for entry in manifest["captures"]))
            rail = (output / "rail_settlement.html").read_text(encoding="utf-8")
            self.assertIn("오송", rail)
            self.assertIn("서울", rail)
            self.assertIn("용산 → 오송", rail)
            self.assertIn("사용자 입력", rail)
            manuals = [event for event in seen if event["type"] == "manual"]
            self.assertTrue(any(event["collected_count"] == 0 and "아직 없습니다" in event["message"] for event in manuals))
            self.assertEqual(manuals[-1]["collected_count"], 3)
            self.assertFalse(worker.stopping.is_set())

    def test_invalid_pdf_capture_refresh_retry_and_empty_finish_stay_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "invalid-development.pdf"
            source.write_bytes(b"NOT A PDF - DEVELOPMENT FIXTURE")
            worker, seen, done, output, manifest = self.run_request(root, (
                ("import_pdf", (source, "", False)), ("capture", "stale-managed-page"),
                ("refresh", None), ("retry_korail", None), ("finish_receipts", None), ("skip", None),
            ))
            self.assertEqual(done["count"], 0)
            self.assertEqual(done["skipped_sites"], ["korail"])
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["captures"], [])
            self.assertEqual(list(output.glob("*.png")), [])
            self.assertEqual(source.read_bytes(), b"NOT A PDF - DEVELOPMENT FIXTURE")
            manuals = [event for event in seen if event["type"] == "manual"]
            self.assertTrue(any("올바른 PDF" in event["message"] for event in manuals))
            self.assertTrue(any("일반 브라우저 영수증은" in event["message"] for event in manuals))
            self.assertTrue(any("아직 없습니다" in event["message"] for event in manuals))
            self.assertTrue(all(event["collected_count"] == 0 for event in manuals))
            self.assertFalse(worker.cancelled.is_set())
            self.assertFalse(worker.stopping.is_set())

    def test_explicit_external_window_original_survives_skip_after_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "app" / "staging"
            staging.mkdir(parents=True)
            source = staging / "development-window.png"
            image = Image.new("RGB", (600, 200), "white")
            ImageDraw.Draw(image).text((20, 25), "DEVELOPMENT FIXTURE - NOT A REAL RECEIPT", fill="black")
            image.save(source)
            original = source.read_bytes()
            worker, seen, done, output, manifest = self.run_request(root, (
                ("external", (source, "개발용 가상 영수증 창")), ("skip", None),
            ))
            self.assertEqual(done["count"], 1)
            self.assertEqual(done["skipped_sites"], [])
            self.assertEqual(manifest["status"], "complete")
            entry = manifest["captures"][0]
            self.assertEqual(entry["site"], "korail")
            self.assertEqual((output / entry["image_path"]).read_bytes(), original)
            self.assertEqual(entry["image_sha256"], hashlib.sha256(original).hexdigest())
            self.assertNotIn("승차일", entry["details"])
            self.assertIn("사용자가 직접", entry["details"]["조회 확인"])
            self.assertFalse(source.exists())  # Only the app's staged copy is moved.
            self.assertEqual([event for event in seen if event["type"] == "manual"][-1]["collected_count"], 1)
            self.assertFalse(worker.stopping.is_set())

    def test_unstaged_file_is_not_accepted_as_a_window_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "outside-stage.png"
            Image.new("RGB", (300, 200), "white").save(source)
            original = source.read_bytes()
            _, seen, done, output, manifest = self.run_request(root, (
                ("external", (source, "개발용 미승인 경로")), ("skip", None),
            ))
            self.assertEqual(done["count"], 0)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(output.glob("*.png")), [])
            self.assertTrue(any(event["type"] == "manual" and "가져오지 못했습니다" in event["message"] for event in seen))

    def test_browserless_capture_does_not_fall_back_to_a_stale_managed_tab(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[Mock()])
        with patch.object(worker, "_choose_page") as choose:
            with self.assertRaisesRegex(RuntimeError, "일반 브라우저 영수증 저장"):
                worker._capture("korail", PreparedPage(None), Path("unused"), manual=True)
        choose.assert_not_called()

    def test_packaging_selfcheck_drives_the_actual_worker_events(self):
        from main import self_check_manual_korail

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic.pdf"
            source.write_bytes(synthetic_pdf())
            output = root / "check"
            self_check_manual_korail(source, output)
            result = json.loads((output / "runtime_check.json").read_text(encoding="utf-8"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["browser_launch_attempts"], 0)
            self.assertTrue(result["waiting_before_browser"])
            self.assertTrue(result["emptyfinish_keptwaiting"])
            self.assertEqual(result["manual_counts"], [0, 0, 3])
            self.assertEqual(result["pages"], 3)
            self.assertTrue(result["original_unchanged"])


if __name__ == "__main__":
    unittest.main()
