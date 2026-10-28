"""Separate car/train requests must produce separate evidence packages."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trip_capture.models import CaptureRecord, RunRequest, TRANSPORT_LABELS
from trip_capture.report import write_report


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
CAR_SITES = ("naver", "opinet", "hipass")
TRAIN_SITES = ("korail",)


class TripModeRequestTests(unittest.TestCase):
    def request(self, **changes):
        values = {
            "trip_date": "2026-09-30", "origin": "개발용 출발지", "destination": "개발용 도착지",
        }
        values.update(changes)
        return RunRequest(**values)

    def test_explicit_modes_accept_only_their_own_evidence_sources(self):
        for mode, sites in (("car", CAR_SITES), ("train", TRAIN_SITES)):
            with self.subTest(mode=mode):
                request = self.request(transport_mode=mode, sites=sites)
                request.validate()
                self.assertEqual(request.resolved_transport_mode, mode)

    def test_explicit_car_accepts_a_selected_subset(self):
        request = self.request(transport_mode="car", sites=("opinet",))
        request.validate()
        self.assertEqual(request.resolved_transport_mode, "car")

    def test_explicit_modes_reject_mixed_or_opposite_sources(self):
        cases = (
            ("car", TRAIN_SITES), ("car", CAR_SITES + TRAIN_SITES),
            ("train", CAR_SITES), ("train", TRAIN_SITES + ("hipass",)),
        )
        for mode, sites in cases:
            with self.subTest(mode=mode, sites=sites):
                with self.assertRaises(ValueError):
                    self.request(transport_mode=mode, sites=sites).validate()

    def test_unknown_mode_is_rejected(self):
        for mode in ("bus", "CAR", "train "):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.request(transport_mode=mode).validate()

    def test_empty_evidence_selection_is_rejected_for_both_modes(self):
        for mode in ("car", "train"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.request(transport_mode=mode, sites=()).validate()

    def test_legacy_requests_infer_mode_without_changing_compatibility(self):
        for sites, expected in ((CAR_SITES, "car"), (TRAIN_SITES, "train"), (CAR_SITES + TRAIN_SITES, "train")):
            with self.subTest(sites=sites):
                request = self.request(sites=sites)
                request.validate()
                self.assertEqual(request.transport_mode, "")
                self.assertEqual(request.resolved_transport_mode, expected)


class TripModeReportTests(unittest.TestCase):
    def request(self, mode: str) -> RunRequest:
        return RunRequest(
            trip_date="2026-09-30", trip_end_date="2026-10-01",
            origin="개발용 세종", destination="개발용 서울", transport_mode=mode,
            sites=CAR_SITES if mode == "car" else TRAIN_SITES,
            department="개발용 가상부서", traveler_name="개발용 가상인물", trip_purpose="검증용 가상 출장",
            rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송",
        )

    def records(self, root: Path, sites: tuple[str, ...]) -> list[CaptureRecord]:
        records = []
        for site in sites:
            image = root / f"development_{site}.png"
            image.write_bytes(PNG)
            records.append(CaptureRecord(
                site, f"개발용 가상자료 · {site}", image, "", "2026-10-23T12:00:00+09:00",
                details={"자료 구분": "개발용 가상자료"}, manual=True,
            ))
        return records

    def test_complete_reports_have_mode_specific_titles_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            for mode, expected, opposite in (
                ("car", "car_settlement.html", "rail_settlement.html"),
                ("train", "rail_settlement.html", "car_settlement.html"),
            ):
                with self.subTest(mode=mode):
                    root = Path(temporary) / mode
                    root.mkdir()
                    request = self.request(mode)
                    records = self.records(root, request.sites)
                    before = {record.image_path: record.image_path.read_bytes() for record in records}
                    main = write_report(request, records, root)
                    content = main.read_text(encoding="utf-8")
                    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                    self.assertIn(f"<h1>{TRANSPORT_LABELS[mode]} 출장보고서</h1>", content)
                    self.assertEqual(manifest["trip"]["transport_mode"], mode)
                    self.assertEqual(manifest["trip"]["transport_label"], TRANSPORT_LABELS[mode])
                    self.assertEqual(manifest["status"], "complete")
                    self.assertTrue((root / expected).is_file())
                    self.assertIn(expected, content)
                    self.assertFalse((root / opposite).exists())
                    self.assertNotIn(opposite, content)
                    for record, entry in zip(records, manifest["captures"]):
                        self.assertEqual(record.image_path.read_bytes(), before[record.image_path])
                        self.assertEqual(entry["image_sha256"], hashlib.sha256(PNG).hexdigest())
                        self.assertIn("data:image/png;base64," + base64.b64encode(PNG).decode("ascii"), content)
                    if mode == "train":
                        sidecar = (root / expected).read_text(encoding="utf-8")
                        self.assertNotIn("유종", content + sidecar)
                        self.assertNotIn("자동차용경유", content + sidecar)
                        self.assertNotIn("fuel", manifest["trip"])
                        self.assertNotIn("fuel_label", manifest["trip"])

    def test_missing_car_and_train_evidence_are_explicitly_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            for mode in ("car", "train"):
                with self.subTest(mode=mode):
                    root = Path(temporary) / mode
                    root.mkdir()
                    request = self.request(mode)
                    # This is the user's common partial-car case and an empty train case.
                    sites = ("naver", "opinet") if mode == "car" else ()
                    main = write_report(request, self.records(root, sites), root, ["개발용: 마지막 자료를 건너뛰었습니다."])
                    content = main.read_text(encoding="utf-8")
                    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                    expected_site = "하이패스 영수증" if mode == "car" else "코레일 KTX 영수증"
                    self.assertEqual(manifest["status"], "partial" if mode == "car" else "failed")
                    self.assertTrue(any(expected_site in message and "포함되지 않았습니다" in message for message in manifest["notices"]))
                    self.assertIn(expected_site, content)
                    self.assertIn("건너뛰기", content)
                    if mode == "train":
                        self.assertIn("자료 생성 실패", content)
                        self.assertIn("저장된 코레일 영수증이 없습니다", (root / "rail_settlement.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
