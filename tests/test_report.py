from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from trip_capture.models import CaptureRecord, RunRequest
from trip_capture.report import sanitize_source_url, write_report


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF1sAAAAASUVORK5CYII="
)


class ReportTests(unittest.TestCase):
    def test_rail_settlement_keeps_all_original_images_and_labels_fallback_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                "2026-10-20", "세종", "서울 출장지", sites=("korail",),
                trip_end_date="2026-10-22", department='<script>alert("dept")</script>',
                position="주무관", traveler_name='홍<길동> & 김', trip_purpose='현장 "확인"',
                rail_origin="사용자 출발역", rail_destination='<img src=x onerror="x">',
                rail_return_origin='<script>alert("return")</script>', rail_return_destination='오송 & 확인',
            )
            records = []
            for index in range(5):
                image = root / f"receipt_{index}.png"
                image.write_bytes(PNG)
                details = (
                    {"승차일": "2026-10-21", "출발역": "오송", "도착역": "서울"}
                    if index == 0 else {"trip_date": "2099-01-01", "origin": "추정하지마역", "destination": "추정금지역"}
                )
                records.append(CaptureRecord("korail", f"철도 증빙 {index + 1}", image, "https://www.korail.com/", "2026-10-23T11:00:00+09:00", details=details, manual=True))
            main = write_report(request, records, root)
            rail = (root / "rail_settlement.html").read_text(encoding="utf-8")
            self.assertEqual(rail.count('class="sheet"'), 3)
            self.assertEqual(rail.count("data:image/png;base64,"), 5)
            self.assertIn("repeat(2, minmax(0, 1fr))", rail)
            self.assertIn("출장운임정산 증빙영수증", rail)
            self.assertIn("2026-10-21", rail)
            self.assertIn("영수증 화면 확인", rail)
            self.assertIn("사용자 입력 출장기간", rail)
            self.assertIn("2026년 10월 20일 ~ 2026년 10월 22일", rail)
            self.assertNotIn("2099-01-01", rail)
            self.assertNotIn("추정하지마역", rail)
            self.assertNotIn("추정금지역", rail)
            self.assertNotIn("<script>", rail)
            self.assertNotIn("<img src=x", rail)
            self.assertIn("&lt;script&gt;", rail)
            self.assertIn("홍&lt;길동&gt; &amp; 김", rail)
            self.assertIn("&lt;img src=x", rail)
            self.assertIn("귀환 요청 경로(사용자 입력 기준): &lt;script&gt;", rail)
            self.assertIn("오송 &amp; 확인", rail)
            self.assertIn("PDF 페이지별 방향은 자동 지정하지 않습니다", rail)
            fare_tables = [part.split("</table>", 1)[0] for part in rail.split('<table class="fares">')[1:]]
            self.assertTrue(all("alert(&quot;return&quot;)" not in table for table in fare_tables))
            for value in rail.split("data:image/png;base64,")[1:]:
                self.assertEqual(base64.b64decode(value.split('"', 1)[0]), PNG)
            self.assertTrue(all(record.image_path.read_bytes() == PNG for record in records))
            self.assertIn('href="rail_settlement.html"', main.read_text(encoding="utf-8"))
            manifest_text = (root / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["rail_settlement_file"], "rail_settlement.html")
            self.assertEqual(manifest["trip"]["traveler_name"], request.traveler_name)
            self.assertEqual(manifest["trip"]["rail_return_origin"], request.rail_return_origin)
            self.assertEqual(manifest["trip"]["rail_return_destination"], request.rail_return_destination)
            self.assertNotIn("<script>", manifest_text)

    def test_rail_settlement_without_receipts_is_explicit_about_missing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest("2026-10-20", "세종", "서울", sites=("korail",))
            main = write_report(request, [], root)
            rail = (root / "rail_settlement.html").read_text(encoding="utf-8")
            self.assertIn("저장된 코레일 영수증이 없습니다", rail)
            self.assertNotIn("data:image/", rail)
            self.assertIn("자료 생성 실패", main.read_text(encoding="utf-8"))

    def test_rail_return_defaults_to_reversed_requested_stations_without_assigning_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "receipt.png"
            image.write_bytes(PNG)
            request = RunRequest("2026-10-20", "세종", "서울", sites=("korail",), rail_origin="오송", rail_destination="서울")
            record = CaptureRecord("korail", "PDF 자료", image, "", "2026-10-23", manual=True)
            write_report(request, [record], root)
            rail = (root / "rail_settlement.html").read_text(encoding="utf-8")
            self.assertIn("귀환 요청 경로(사용자 입력 기준): 서울 → 오송", rail)
            fares = rail.split('<table class="fares">', 1)[1].split('</table>', 1)[0]
            self.assertIn('오송<small class="basis">사용자 입력 출발역', fares)
            self.assertIn('서울<small class="basis">사용자 입력 도착역', fares)

    def test_escapes_untrusted_text_and_removes_source_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "screen.png"
            image.write_bytes(PNG)
            request = RunRequest(
                trip_date="2026-09-30", origin='<script>alert("origin")</script>',
                destination="도착 & 확인", sites=("naver",), hipass_id="PRIVATE_ACCOUNT",
            )
            record = CaptureRecord(
                site="naver", title='<img src=x onerror="alert(1)">', image_path=image,
                url="https://name:PRIVATE_PASSWORD@example.com/map?route=seoul&access_token=PRIVATE_TOKEN&SESSION=PRIVATE_SESSION#token=PRIVATE_FRAGMENT",
                captured_at="2026-10-19T15:25:30+09:00",
                details={
                    "<상세>": '<script>alert("detail")</script>',
                    "account": "PRIVATE_DETAILS_ACCOUNT",
                    "url": "https://example.com/view?token=PRIVATE_DETAILS_TOKEN&date=20260930",
                    "인쇄 페이지": "https://example.com/print?password=PRIVATE_DETAILS_PASSWORD",
                },
            )
            report = write_report(request, [record], root, ['<img src=x onerror="alert(2)">'])
            content = report.read_text(encoding="utf-8")
            manifest_content = (root / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("<script>", content)
            self.assertNotIn('<img src=x', content)
            self.assertIn("&lt;script&gt;", content)
            self.assertIn("&lt;상세&gt;", content)
            self.assertNotIn("<script>", manifest_content)
            for secret in (
                "PRIVATE_ACCOUNT", "PRIVATE_PASSWORD", "PRIVATE_TOKEN", "PRIVATE_SESSION", "PRIVATE_FRAGMENT",
                "PRIVATE_DETAILS_ACCOUNT", "PRIVATE_DETAILS_TOKEN", "PRIVATE_DETAILS_PASSWORD",
            ):
                self.assertNotIn(secret, content + manifest_content)
            metadata = json.loads(manifest_content)
            self.assertEqual(metadata["trip"]["origin"], request.origin)
            self.assertIn("route=seoul", metadata["captures"][0]["source_url"])
            self.assertIn("date=20260930", metadata["captures"][0]["details"]["url"])
            self.assertEqual(sanitize_source_url("javascript:alert(1)"), "")

    def test_original_image_is_embedded_and_metadata_is_serializable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "original.png"
            image.write_bytes(PNG)
            request = RunRequest(trip_date="2026-09-30", origin="세종", destination="무안", sites=("naver",))
            record = CaptureRecord(
                "naver", "자동차 경로", image, "https://map.naver.com/", "2026-10-19T12:00:00+09:00",
                details={"조회 기준": "캡처 시점"}, manual=True,
            )
            report = write_report(request, [record], root)
            content = report.read_text(encoding="utf-8")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            encoded_image = content.split("data:image/png;base64,", 1)[1].split('"', 1)[0]
            self.assertEqual(base64.b64decode(encoded_image), PNG)
            self.assertEqual(image.read_bytes(), PNG)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["captures"][0]["image_path"], "original.png")
            self.assertEqual(manifest["captures"][0]["image_sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertTrue(manifest["captures"][0]["manual"])
            self.assertIn("2026년 9월 30일", content)
            self.assertIn("2026년 10월 19일 12:00:00 (UTC+09:00)", content)
            self.assertIn("과거 출장일의 실제 운행 경로", content)

    def test_no_captures_and_missing_image_are_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(trip_date="2026-09-30", origin="세종", destination="무안", sites=("opinet",))
            for records in ([], [CaptureRecord("opinet", "유가", root / "missing.png", "https://opinet.co.kr/", "2026-10-19")]):
                with self.subTest(records=len(records)):
                    report = write_report(request, records, root, ["사용자가 건너뛰었습니다."])
                    content = report.read_text(encoding="utf-8")
                    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                    self.assertIn("자료 생성 실패", content)
                    self.assertIn("출장 증빙으로 사용할 수 없습니다", content)
                    self.assertEqual(manifest["status"], "failed")
                    self.assertNotIn("data:image/", content)


if __name__ == "__main__":
    unittest.main()
