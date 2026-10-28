"""Verify car/train report isolation using developer images and a synthetic PDF.

This runner opens no browser, uses no account, and never reads a download folder.
Pass a deliberately fictional PDF fixture; its bytes are copied without editing.
Run: python tools/verify_trip_modes.py SYNTHETIC_PDF OUTPUT_DIRECTORY
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any
import uuid


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trip_capture.models import CaptureRecord, RunRequest, TRANSPORT_LABELS
from trip_capture.pdf_receipts import import_receipt_pdf
from trip_capture.report import write_report


CAR_SITES = ("naver", "opinet", "hipass")
TRAIN_SITES = ("korail",)
FIXTURE_NOTICE = "개발용 가상자료입니다. 실제 출장 증빙으로 제출할 수 없습니다."


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(mode: str, output_dir: Path) -> RunRequest:
    return RunRequest(
        trip_date="2026-09-30", trip_end_date="2026-10-01",
        origin="개발용 가상 출발지", destination="개발용 가상 출장지", fuel="diesel",
        transport_mode=mode, sites=CAR_SITES if mode == "car" else TRAIN_SITES,
        output_dir=output_dir, department="개발용 가상 부서", position="개발용 검증 직급",
        traveler_name="개발용 가상 인물", trip_purpose="자동화 소프트웨어의 두 출장 유형 분리 검증",
        rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송",
    )


def _developer_png(path: Path, site: str) -> None:
    # These are conspicuously labelled test cards, never reconstructed receipts.
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1200, 420), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    small = ImageFont.load_default(size=25)
    draw.rectangle((8, 8, 1191, 411), outline="#b22128", width=10)
    draw.text((45, 45), "DEVELOPMENT FIXTURE - SYNTHETIC DATA", fill="#b22128", font=font)
    draw.text((45, 130), f"CAR MODE / {site.upper()}", fill="#16334f", font=font)
    draw.text((45, 220), "Not a website screenshot. Not valid travel evidence.", fill="black", font=small)
    draw.text((45, 290), "2026-09-30 / fictional input / offline verification only", fill="black", font=small)
    image.save(path)


def _car_png_records(directory: Path) -> list[CaptureRecord]:
    records = []
    for site in ("naver", "opinet"):
        image = directory / f"development_synthetic_{site}.png"
        _developer_png(image, site)
        details = {"자료 구분": FIXTURE_NOTICE}
        if site == "opinet":
            details.update({"trip_date": "2026-09-30", "fuel": "자동차용경유", "verified_date": "2026-09-30"})
        records.append(CaptureRecord(
            site=site, title=f"개발용 가상자료 · {site}", image_path=image,
            url="", captured_at="2026-10-23T12:00:00+09:00", details=details, manual=True,
        ))
    return records


def _embedded_hashes(content: str) -> Counter[str]:
    return Counter(
        hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
        for encoded in re.findall(r'data:image/[^;"\s]+;base64,([^"\s]+)', content)
    )


def _verify_bundle(
    request: RunRequest,
    records: list[CaptureRecord],
    directory: Path,
    *,
    expected_status: str,
    expected_pdf_hash: str | None = None,
    expected_pdf_pages: int = 0,
) -> dict[str, Any]:
    mode = request.resolved_transport_mode
    expected_file = "car_settlement.html" if mode == "car" else "rail_settlement.html"
    opposite_file = "rail_settlement.html" if mode == "car" else "car_settlement.html"
    originals = {record.image_path: _sha256(record.image_path) for record in records}
    original_pdfs = {record.original_path: _sha256(record.original_path) for record in records if record.original_path is not None}
    # The fixture warning is visible, but it must not convert a complete report
    # into "partial" merely because the verifier supplied a notice.
    main = write_report(request, records, directory)
    content = main.read_text(encoding="utf-8")
    sidecar = (directory / expected_file).read_text(encoding="utf-8")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    _require(f"<h1>{TRANSPORT_LABELS[mode]} 출장보고서</h1>" in content, f"{mode}: wrong main report heading")
    _require(manifest["trip"].get("transport_mode") == mode, f"{mode}: wrong manifest transport_mode")
    _require(manifest["trip"].get("transport_label") == TRANSPORT_LABELS[mode], f"{mode}: wrong manifest transport_label")
    _require(manifest.get("status") == expected_status, f"{mode}: unexpected report status {manifest.get('status')!r}")
    _require(expected_file in content, f"{mode}: missing settlement link")
    _require(not (directory / opposite_file).exists(), f"{mode}: opposite settlement file was generated")
    _require(opposite_file not in content, f"{mode}: opposite settlement linked")
    _require(set(entry["site"] for entry in manifest["captures"]) <= set(request.sites), f"{mode}: opposite evidence source included")

    expected_images = Counter(originals.values())
    _require(_embedded_hashes(content) == expected_images, f"{mode}: main report omitted or changed an original image")
    _require(_embedded_hashes(sidecar) == expected_images, f"{mode}: settlement omitted or changed an original image")
    for path, checksum in originals.items():
        _require(_sha256(path) == checksum, f"{mode}: original image bytes changed: {path.name}")
    for entry in manifest["captures"]:
        path = (directory / entry["image_path"]).resolve()
        _require(entry.get("image_sha256") == originals[path], f"{mode}: incorrect manifest image hash")
    for path, checksum in original_pdfs.items():
        _require(_sha256(path) == checksum == expected_pdf_hash, f"{mode}: original PDF bytes changed")

    pdf_records = [record for record in records if record.original_path is not None]
    _require(len(pdf_records) == expected_pdf_pages, f"{mode}: incomplete PDF page set")
    attachments = manifest.get("attachments", [])
    _require(len(attachments) == (1 if expected_pdf_pages else 0), f"{mode}: unexpected PDF original count")
    for attachment in attachments:
        _require(attachment["sha256"] == expected_pdf_hash, f"{mode}: incorrect original PDF manifest hash")
        _require(_sha256(directory / attachment["path"]) == expected_pdf_hash, f"{mode}: linked original PDF not preserved")
    if mode == "train":
        _require("유종" not in content + sidecar, "train: fuel field leaked into train report")
        _require("자동차용경유" not in content + sidecar, "train: diesel label leaked into train report")
        _require("fuel" not in manifest["trip"] and "fuel_label" not in manifest["trip"], "train: car fuel metadata leaked")
    else:
        _require("자동차용경유" in content, "car: requested fuel missing")

    missing_sites = set(request.sites) - {record.site for record in records}
    if missing_sites:
        from trip_capture.models import SITE_LABELS
        for site in missing_sites:
            _require(any(SITE_LABELS[site] in item and "포함되지 않았습니다" in item for item in manifest["notices"]), f"{mode}: missing evidence notice not recorded for {site}")
        _require("건너뛰기" in content, f"{mode}: missing-evidence guidance absent")
    if not records:
        _require("자료 생성 실패" in content, f"{mode}: empty report looks successful")
        _require(not _embedded_hashes(content), f"{mode}: empty report contains invented images")
    return {
        "mode": mode, "status": expected_status, "main_report": str(main.resolve()),
        "settlement": str((directory / expected_file).resolve()), "manifest": str((directory / "manifest.json").resolve()),
        "images_verified": len(records), "pdf_pages_verified": len(pdf_records),
        "original_pdf_sha256": expected_pdf_hash, "missing_sites": sorted(missing_sites),
        "opposite_settlement_absent": True, "original_images_preserved": True,
    }


def verify_modes(source_pdf: str | Path, outputdir: str | Path) -> dict[str, Any]:
    """Generate and verify four offline scenarios; return JSON-safe result paths.

    The supplied PDF must be a development fixture. Each call creates a new run
    subdirectory, preserving previous results and the input file unchanged.
    """
    import pypdfium2 as pdfium

    source = Path(source_pdf).resolve(strict=True)
    source_checksum = _sha256(source)
    document = pdfium.PdfDocument(str(source))
    try:
        page_count = len(document)
    finally:
        document.close()
    _require(page_count > 0, "The supplied development PDF has no pages")
    output = Path(outputdir).resolve() / f"trip_modes_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    output.mkdir(parents=True, exist_ok=False)
    (output / "DEVELOPMENT_FIXTURES_ONLY.txt").write_text(FIXTURE_NOTICE + "\n", encoding="utf-8")
    scenarios = []
    for mode in ("car", "train"):
        directory = output / mode
        directory.mkdir()
        request = _request(mode, directory)
        request.validate()
        records = _car_png_records(directory) if mode == "car" else []
        pdf_site = "hipass" if mode == "car" else "korail"
        pdf_records = import_receipt_pdf(source, directory, site=pdf_site, captured_at="2026-10-23T12:00:00+09:00")
        for record in pdf_records:
            record.title = "개발용 가상자료 · " + record.title
            record.details["자료 구분"] = FIXTURE_NOTICE
        records.extend(pdf_records)
        scenarios.append(_verify_bundle(request, records, directory, expected_status="complete", expected_pdf_hash=source_checksum, expected_pdf_pages=page_count))
    for mode in ("car", "train"):
        directory = output / f"{mode}_missing"
        directory.mkdir()
        records = _car_png_records(directory) if mode == "car" else []
        scenarios.append(_verify_bundle(_request(mode, directory), records, directory, expected_status="partial" if mode == "car" else "failed"))
    _require(_sha256(source) == source_checksum, "Input development PDF was modified")
    result = {
        "ok": True, "development_fixture_only": True, "fixture_notice": FIXTURE_NOTICE,
        "source_pdf": str(source), "source_pdf_sha256": source_checksum, "source_pdf_pages": page_count,
        "source_unchanged": True, "output_directory": str(output), "scenarios": scenarios,
    }
    (output / "verification-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path, help="이미 준비된 개발용 가상 PDF 자료")
    parser.add_argument("outputdir", type=Path, help="개발용 검증 결과를 저장할 폴더")
    arguments = parser.parse_args()
    print(json.dumps(verify_modes(arguments.source_pdf, arguments.outputdir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
