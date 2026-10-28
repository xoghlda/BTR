"""Preserve a selected PDF and render every receipt page without editing it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import uuid

from .models import CaptureRecord
from .report import sanitize_source_url


RENDER_DPI = 150
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_PAGE_PIXELS = 30_000_000
MAX_TOTAL_PIXELS = 150_000_000
KST = timezone(timedelta(hours=9))


class ReceiptPdfError(ValueError):
    """An actionable import failure; no partial import is returned."""


def import_receipt_pdf(
    source: Path,
    output_dir: Path,
    *,
    site: str = "hipass",
    source_url: str = "",
    captured_at: str | None = None,
) -> list[CaptureRecord]:
    """Copy the exact PDF bytes and return one PNG-backed record per page.

    All generated files are rolled back if any page cannot be imported. The
    user's source file is only read, even when it is already in output_dir.
    Supported sites are hipass (the backwards-compatible default) and korail.
    """
    labels = {"hipass": "하이패스", "korail": "코레일"}
    if site not in labels:
        raise ReceiptPdfError("PDF 영수증의 자료 구분을 확인해 주세요. 하이패스 또는 코레일만 지원합니다.")
    label = labels[site]
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ReceiptPdfError("PDF 처리 구성요소(pypdfium2)가 없습니다. 프로그램을 다시 설치하거나 개발 환경의 의존성을 설치해 주세요.") from exc

    source = Path(source).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    created: list[Path] = []
    document = None
    try:
        if not source.is_file():
            raise ReceiptPdfError("선택한 PDF 파일을 찾을 수 없습니다. 파일을 다시 선택해 주세요.")
        if source.stat().st_size > MAX_PDF_BYTES:
            raise ReceiptPdfError("PDF가 너무 큽니다. 100 MB 이하의 영수증 PDF를 선택해 주세요.")
        original_bytes = source.read_bytes()
        if not original_bytes:
            raise ReceiptPdfError("빈 PDF 파일입니다. 영수증을 PDF로 다시 저장해 주세요.")
        if len(original_bytes) > MAX_PDF_BYTES:
            raise ReceiptPdfError("PDF가 너무 큽니다. 100 MB 이하의 영수증 PDF를 선택해 주세요.")
        if b"%PDF-" not in original_bytes[:1024]:
            raise ReceiptPdfError(f"올바른 PDF 파일이 아닙니다. {label}에서 영수증을 PDF로 다시 저장해 주세요.")
        try:
            document = pdfium.PdfDocument(original_bytes)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
                raise ReceiptPdfError("암호가 설정된 PDF입니다. 암호가 없는 영수증 PDF로 저장한 뒤 다시 선택해 주세요.") from exc
            raise ReceiptPdfError("PDF가 손상되었거나 읽을 수 없습니다. 영수증 PDF를 다시 저장해 주세요.") from exc
        # If PDFium opens the document without a password, its visible pages
        # can be rendered. Preserve the encrypted source bytes unchanged;
        # owner-password restrictions do not require rewriting the PDF.
        page_count = len(document)
        if page_count == 0:
            raise ReceiptPdfError("PDF에 페이지가 없습니다. 영수증 PDF를 다시 저장해 주세요.")
        if page_count > MAX_PDF_PAGES:
            raise ReceiptPdfError(f"PDF가 {page_count}페이지입니다. 한 번에 {MAX_PDF_PAGES}페이지 이하의 영수증을 선택해 주세요.")
        scale = RENDER_DPI / 72
        total_pixels = 0
        for page_index in range(page_count):
            page = document.get_page(page_index)
            try:
                width, height = page.get_size()
                if not all(math.isfinite(size) and size > 0 for size in (width, height)):
                    raise ReceiptPdfError(f"PDF {page_index + 1}페이지의 크기가 올바르지 않습니다.")
                pixel_width, pixel_height = math.ceil(width * scale), math.ceil(height * scale)
                pixels = pixel_width * pixel_height
                total_pixels += pixels
                if max(pixel_width, pixel_height) > 20_000 or pixels > MAX_PAGE_PIXELS:
                    raise ReceiptPdfError(f"PDF {page_index + 1}페이지의 크기가 너무 큽니다. 영수증을 일반 용지 크기로 다시 저장해 주세요.")
                if total_pixels > MAX_TOTAL_PIXELS:
                    raise ReceiptPdfError("PDF 전체 페이지의 이미지 크기가 너무 큽니다. 영수증을 나누어 저장한 뒤 다시 선택해 주세요.")
            finally:
                page.close()
        document.init_forms()
        output_dir.mkdir(parents=True, exist_ok=True)
        identifier = uuid.uuid4().hex
        original_path = output_dir / f"{site}_receipt_{identifier}.pdf"
        with original_path.open("xb") as file:
            created.append(original_path)
            file.write(original_bytes)
        records: list[CaptureRecord] = []
        timestamp = captured_at or datetime.now(KST).isoformat(timespec="seconds")
        safe_url = sanitize_source_url(source_url)
        for page_index in range(page_count):
            page = document.get_page(page_index)
            bitmap = None
            image = None
            try:
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                image_path = output_dir / f"{site}_receipt_{identifier}_page_{page_index + 1:03d}.png"
                with image_path.open("xb") as file:
                    created.append(image_path)
                    image.save(file, format="PNG", dpi=(RENDER_DPI, RENDER_DPI))
                records.append(CaptureRecord(
                    site=site,
                    title=f"{label} 영수증 PDF · {page_index + 1}/{page_count}페이지",
                    image_path=image_path,
                    url=safe_url,
                    captured_at=timestamp,
                    details={
                        "저장 방식": "원본 PDF 페이지 이미지",
                        "가져온 PDF 이름": source.name,
                        "원본 PDF 파일": original_path.name,
                        "PDF 페이지": f"{page_index + 1}/{page_count}",
                        "페이지 변환": f"원본 PDF의 모든 페이지를 {RENDER_DPI} DPI PNG로 변환했습니다. 화면 캡처가 아닙니다.",
                    },
                    manual=True,
                    original_path=original_path,
                ))
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
        return records
    except Exception as exc:
        for generated in reversed(created):
            try:
                generated.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, ReceiptPdfError):
            raise
        if isinstance(exc, OSError):
            raise ReceiptPdfError("PDF 파일을 읽거나 결과를 저장하지 못했습니다. 파일 접근 권한과 저장 공간을 확인해 주세요.") from exc
        raise ReceiptPdfError("PDF의 모든 페이지를 변환하지 못했습니다. 생성한 파일은 취소했으며 원본 PDF는 변경하지 않았습니다.") from exc
    finally:
        if document is not None:
            document.close()
