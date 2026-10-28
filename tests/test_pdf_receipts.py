from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pypdfium2

from trip_capture.models import RunRequest
from trip_capture.pdf_receipts import MAX_PDF_PAGES, ReceiptPdfError, import_receipt_pdf
from trip_capture.report import write_report


def sample_pdf(page_count: int = 3) -> bytes:
    """Create a readable multipage PDF with only the Python standard library."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Count {page_count} /Kids [" + " ".join(f"{4 + index * 2} 0 R" for index in range(page_count)) + "] >>").encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(page_count):
        page_id = 4 + index * 2
        stream = (
            f"BT /F1 22 Tf 24 368 Td (RECEIPT PAGE {index + 1}) Tj ET\n"
            f"BT /F1 14 Tf 24 330 Td (Total: {1200 + index * 100} KRW) Tj ET\n"
            f"{0.15 + (index % 3) * 0.25:.2f} 0.25 0.65 rg 24 230 240 48 re f\n"
            "0 0 0 RG 24 220 240 68 re S\n"
        ).encode("ascii")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 420] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode("ascii"),
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


class ReceiptPdfTests(unittest.TestCase):
    def test_korail_pdf_uses_own_site_title_and_prefix_without_losing_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ktx.pdf"
            original = sample_pdf(3)
            source.write_bytes(original)
            output = root / "report"
            records = import_receipt_pdf(source, output, site="korail")
            self.assertEqual(len(records), 3)
            self.assertTrue(all(record.site == "korail" for record in records))
            self.assertTrue(all(record.image_path.name.startswith("korail_receipt_") for record in records))
            self.assertEqual(records[2].title, "코레일 영수증 PDF · 3/3페이지")
            self.assertEqual(records[0].original_path.read_bytes(), original)
            request = RunRequest("2026-10-20", "세종", "서울", sites=("korail",), rail_origin="오송", rail_destination="서울")
            write_report(request, records, output)
            rail = (output / "rail_settlement.html").read_text(encoding="utf-8")
            self.assertEqual(rail.count('class="sheet"'), 2)
            self.assertEqual(rail.count("data:image/png;base64,"), 3)
            self.assertIn("원본 PDF 페이지 변환 이미지", rail)
            with self.assertRaisesRegex(ReceiptPdfError, "자료 구분"):
                import_receipt_pdf(source, root / "unsupported", site="../other")
            self.assertFalse((root / "unsupported").exists())

    def test_all_pages_render_original_is_identical_and_report_links_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'receipt & original.pdf'
            source_bytes = sample_pdf(3)
            source.write_bytes(source_bytes)
            destination = root / "report"
            records = import_receipt_pdf(
                source, destination,
                source_url="https://www.hipass.co.kr/receipt?token=DO_NOT_RECORD&date=20260930",
                captured_at="2026-10-19T12:30:00+09:00",
            )
            self.assertEqual(len(records), 3)
            self.assertEqual(len(list(destination.glob("*.png"))), 3)
            self.assertEqual(len(list(destination.glob("*.pdf"))), 1)
            original = records[0].original_path
            self.assertIsNotNone(original)
            self.assertEqual(original.read_bytes(), source_bytes)
            self.assertEqual(source.read_bytes(), source_bytes)
            pixel_hashes: set[str] = set()
            for index, record in enumerate(records, start=1):
                self.assertEqual(record.original_path, original)
                self.assertEqual(record.title, f"하이패스 영수증 PDF · {index}/3페이지")
                self.assertTrue(record.manual)
                self.assertNotIn("DO_NOT_RECORD", record.url)
                self.assertEqual(record.details["저장 방식"], "원본 PDF 페이지 이미지")
                with Image.open(record.image_path) as image:
                    self.assertEqual(image.width, 625)
                    self.assertIn(image.height, (875, 876))  # PDFium rounds fractional pixel bounds up.
                    self.assertAlmostEqual(image.info["dpi"][0], 150, places=1)
                    gray = image.convert("L")
                    # The rendered text area must contain both paper and ink,
                    # not a blank or failed/black renderer result.
                    ink = gray.crop((35, 65, 600, 230))
                    histogram = ink.histogram()
                    self.assertGreater(sum(histogram[:100]), 1000)
                    self.assertGreater(sum(histogram[245:]), 10000)
                    pixel_hashes.add(hashlib.sha256(image.tobytes()).hexdigest())
            self.assertEqual(len(pixel_hashes), 3)
            request = RunRequest("2026-09-30", "세종", "무안", sites=("hipass",))
            report = write_report(request, records, destination)
            content = report.read_text(encoding="utf-8")
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn('href="' + original.name + '"', content)
            self.assertIn("원본 PDF 다운로드", content)
            self.assertIn("화면 캡처가 아니라 원본 PDF", content)
            self.assertEqual(content.count("data:image/png;base64,"), 3)
            self.assertEqual(len(manifest["attachments"]), 1)
            self.assertEqual(manifest["attachments"][0]["sha256"], hashlib.sha256(source_bytes).hexdigest())
            self.assertTrue(all(entry["kind"] == "pdf_page" and entry["original"]["file"] == original.name for entry in manifest["captures"]))
            self.assertNotIn("DO_NOT_RECORD", content + json.dumps(manifest))

    def test_invalid_empty_encrypted_and_excessive_pdfs_have_no_partial_output(self):
        fixtures = [(b"", "빈 PDF"), (b"%PDF-1.7\nnot a PDF", "손상"), (sample_pdf(MAX_PDF_PAGES + 1), "페이지")]
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            pass
        else:
            writer = PdfWriter()
            writer.append_pages_from_reader(PdfReader(io.BytesIO(sample_pdf(1))))
            writer.encrypt("test-password")
            encrypted = io.BytesIO()
            writer.write(encrypted)
            fixtures.append((encrypted.getvalue(), "암호"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "report"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            for data, expected in fixtures:
                with self.subTest(expected=expected):
                    source = root / "source.pdf"
                    source.write_bytes(data)
                    with self.assertRaisesRegex(ReceiptPdfError, expected):
                        import_receipt_pdf(source, destination)
                    self.assertEqual(source.read_bytes(), data)
                    self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_owner_encrypted_pdf_opens_without_password_and_preserves_bytes(self):
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            self.skipTest("이 테스트의 암호 PDF 생성에는 pypdf가 필요합니다.")
        writer = PdfWriter()
        writer.append_pages_from_reader(PdfReader(io.BytesIO(sample_pdf(2))))
        writer.encrypt(user_password="", owner_password="owner-only-test-password")
        buffer = io.BytesIO()
        writer.write(buffer)
        original = buffer.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "owner-encrypted.pdf"
            source.write_bytes(original)
            records = import_receipt_pdf(source, root / "report")
            self.assertEqual(len(records), 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(records[0].original_path.read_bytes(), original)
            self.assertEqual(
                hashlib.sha256(records[0].original_path.read_bytes()).hexdigest(),
                hashlib.sha256(original).hexdigest(),
            )

    def test_failure_on_second_page_rolls_back_only_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            original = sample_pdf(3)
            source.write_bytes(original)
            destination = root / "report"
            destination.mkdir()
            sentinel = destination / "existing.pdf"
            sentinel.write_bytes(b"existing unrelated file")
            real_render = pypdfium2.PdfPage.render
            calls = 0

            def fail_later(page, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated render failure")
                return real_render(page, *args, **kwargs)

            with patch.object(pypdfium2.PdfPage, "render", fail_later):
                with self.assertRaisesRegex(ReceiptPdfError, "모든 페이지"):
                    import_receipt_pdf(source, destination)
            self.assertEqual(calls, 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(sentinel.read_bytes(), b"existing unrelated file")
            self.assertEqual(list(destination.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
