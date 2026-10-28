from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from trip_capture.models import CaptureRecord, RunRequest
from trip_capture.report import write_report


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF1sAAAAASUVORK5CYII="
)


def make_record(root: Path, site: str) -> CaptureRecord:
    image = root / f"{site}.png"
    image.write_bytes(PNG)
    return CaptureRecord(site, f"{site} 원본", image, "https://example.com/", "2026-10-24T12:30:00+09:00", manual=True)


class TransportReportTests(unittest.TestCase):
    def test_car_report_has_only_car_artifact_and_preserves_original_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                "2026-10-20", '<script>alert("origin")</script>', "부산 & 확인", transport_mode="car",
                department='업무<지원>', position="주무관", traveler_name='홍"길동', trip_purpose="출장 현장 확인",
            )
            records = [make_record(root, site) for site in ("naver", "opinet", "hipass")]
            main = write_report(request, records, root).read_text(encoding="utf-8")
            car = (root / "car_settlement.html").read_text(encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("<h1>자차 이용 출장보고서</h1>", main)
            self.assertIn("경로·유가·통행료 증빙", main)
            self.assertIn("<th>유종</th>", main)
            self.assertIn("자차 이용 출장 증빙서", car)
            self.assertIn("업무&lt;지원&gt;", car)
            self.assertNotIn("<script>", main + car)
            self.assertIn("&lt;script&gt;", car)
            self.assertEqual(car.count('class="sheet"'), 3)
            self.assertEqual(car.count("data:image/png;base64,"), 3)
            self.assertFalse((root / "rail_settlement.html").exists())
            self.assertEqual(manifest["car_settlement_file"], "car_settlement.html")
            self.assertIsNone(manifest["rail_settlement_file"])
            self.assertEqual(manifest["trip"]["transport_mode"], "car")
            self.assertEqual(manifest["trip"]["transport_label"], "자차 이용")
            self.assertEqual(manifest["trip"]["fuel"], "diesel")
            for encoded in car.split("data:image/png;base64,")[1:]:
                self.assertEqual(base64.b64decode(encoded.split('"', 1)[0]), PNG)
            self.assertTrue(all(record.image_path.read_bytes() == PNG for record in records))
            self.assertTrue(all(entry["image_sha256"] == hashlib.sha256(PNG).hexdigest() for entry in manifest["captures"]))

    def test_train_report_omits_fuel_and_car_fields_and_links_rail_form(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                "2026-10-20", "도로 출발지 표시 금지", "서울", sites=("korail",), transport_mode="train",
                rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송",
            )
            main = write_report(request, [make_record(root, "korail")], root).read_text(encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("<h1>기차 이용 출장보고서</h1>", main)
            self.assertIn("왕복역·코레일 영수증 증빙", main)
            self.assertIn("오송 → 서울", main)
            self.assertIn("용산 → 오송", main)
            self.assertNotIn("도로 출발지 표시 금지", main)
            self.assertNotIn("<th>유종</th>", main)
            self.assertNotIn("자동차용경유", main)
            self.assertNotIn("경로·유가·통행료 증빙", main)
            self.assertNotIn("네이버 지도", main)
            self.assertNotIn("fuel", manifest["trip"])
            self.assertNotIn("fuel_label", manifest["trip"])
            self.assertEqual(manifest["trip"]["transport_label"], "기차 이용")
            self.assertEqual(manifest["rail_settlement_file"], "rail_settlement.html")
            self.assertIsNone(manifest["car_settlement_file"])
            self.assertFalse((root / "car_settlement.html").exists())

    def test_missing_selected_car_evidence_differs_from_unselected_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest("2026-10-20", "세종", "무안", sites=("naver", "opinet"), transport_mode="car")
            write_report(request, [make_record(root, "naver")], root)
            car = (root / "car_settlement.html").read_text(encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(manifest["site_coverage"]["opinet"]["status"], "missing")
            self.assertEqual(manifest["site_coverage"]["hipass"]["status"], "not_selected")
            self.assertIn("일일 유가: 누락", car)
            self.assertIn("통행료 영수증: 선택하지 않음", car)

    def test_legacy_mixed_records_are_all_preserved_in_main_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest("2026-10-20", "세종", "서울", sites=("naver", "korail"))
            records = [make_record(root, "naver"), make_record(root, "korail")]
            main = write_report(request, records, root).read_text(encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(main.count("data:image/png;base64,"), 2)
            self.assertEqual(len(manifest["captures"]), 2)
            self.assertEqual(manifest["transport_mode"], "train")
            self.assertEqual(manifest["captures"][0]["transport_role"], "additional")
            self.assertEqual(manifest["captures"][1]["transport_role"], "primary")
            self.assertIn("추가 보관 자료", main)


if __name__ == "__main__":
    unittest.main()
