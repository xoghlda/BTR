"""Collect original workflow screenshots/PDFs into the existing trip forms."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import uuid

from .automation import KST, make_output_directory
from .models import CaptureRecord, RunRequest, SITE_LABELS
from .native_capture import NativeBrowserWindow, save_visible_browser_window
from .pdf_receipts import import_receipt_pdf
from .report import sanitize_source_url, write_report


class WorkflowEvidence:
    """Used only by the workflow worker; preserve original bytes and provenance."""

    def __init__(self, request: RunRequest):
        request.validate()
        self.request = request
        self.records: list[CaptureRecord] = []
        self.output_dir: Path | None = None
        self.report: Path | None = None

    def _output(self) -> Path:
        if self.output_dir is None:
            self.output_dir = make_output_directory(self.request)
        return self.output_dir

    def _site(self, site: str) -> None:
        if site not in self.request.sites:
            raise ValueError("현재 출장 유형에서 선택한 사이트의 증빙만 저장할 수 있습니다.")
        if self.report is not None:
            raise ValueError("이미 끝난 작업에는 증빙을 추가할 수 없습니다.")

    def capture(self, window: NativeBrowserWindow, site: str, name: str, url: str) -> None:
        self._site(site)
        image = self._output() / f"{site}_workflow_{uuid.uuid4().hex}.png"
        try:
            save_visible_browser_window(window, image)
        except Exception:
            image.unlink(missing_ok=True)
            raise
        self.records.append(CaptureRecord(
            site=site, title=f"{SITE_LABELS[site]} · {name}", image_path=image,
            url=sanitize_source_url(url), captured_at=datetime.now(KST).isoformat(timespec="seconds"),
            details={"저장 방식": "일반 브라우저 전체 흐름의 화면 저장 단계",
                     "조회 조건": "등록한 좌표·입력값으로 실행했습니다. 출장일·구간·금액은 원본 화면과 대조하세요."},
            manual=True,
        ))

    def import_pdf(self, path: Path, site: str, url: str) -> None:
        self._site(site)
        self.records.extend(import_receipt_pdf(path, self._output(), site=site, source_url=url))

    def finish(self, status: str) -> Path:
        if self.report is not None:
            return self.report
        notices = ["로그인부터 전체 자동화에서 수집한 원본입니다. 조회 성공·출장일·구간·금액은 원본과 대조하세요."]
        if status != "complete":
            notices.append("자동 흐름이 중간에 멈췄습니다. 그때까지 저장한 자료를 보존했으며 누락된 증빙을 확인해야 합니다.")
        self.report = write_report(self.request, self.records, self._output(), notices=notices)
        return self.report
