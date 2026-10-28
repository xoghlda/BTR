from __future__ import annotations

import queue
import hashlib
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .credentials import WindowsCredentialStore
from .models import CaptureRecord, PreparedPage, RunRequest, SITE_LABELS
from .platformpaths import app_data_dir
from .report import sanitize_source_url, write_report

KST = timezone(timedelta(hours=9))
_HOSTS = {"naver": ("map.naver.com",), "opinet": ("opinet.co.kr",), "hipass": ("hipass.co.kr",),
          "korail": ("korail.com", "letskorail.com")}
_RECEIPT_SITES = ("hipass", "korail")
_RECEIPT_URLS = {"hipass": "https://www.hipass.co.kr/", "korail": "https://www.korail.com/ticket/mypage/ticketInfo/receipt"}
_KORAIL_MANUAL_MESSAGE = (
    "일반 Chrome 또는 Edge에서 코레일에 직접 로그인한 뒤 ‘이용내역/영수증 조회’에서 개별 운임영수증을 여세요. "
    "‘영수증 PDF 가져오기’ 또는 ‘일반 브라우저 영수증 저장’으로 가는편·오는편을 추가하고 ‘코레일 수집 완료’를 누르세요."
)


def _ordinary_korail_page(request: RunRequest) -> PreparedPage:
    """Collect explicitly selected originals without connecting to a login session."""
    return PreparedPage(
        page=None, ready=False, message=_KORAIL_MANUAL_MESSAGE,
        details={
            "trip_date": request.trip_date,
            "trip_end_date": request.trip_end_date or request.trip_date,
            "rail_origin": request.rail_origin,
            "rail_destination": request.rail_destination,
            "rail_return_origin": request.rail_return_origin,
            "rail_return_destination": request.rail_return_destination,
            "requested_url": _RECEIPT_URLS["korail"],
            "수집 방식": "일반 브라우저에서 사용자가 선택한 PDF 또는 영수증 창",
        },
    )


def belongs_to_site(url: str, site: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in _HOSTS[site])


def known_page_owner(page) -> str | None:
    """An explicit site or opener takes priority over a temporary stage tag."""
    visited = set()
    for _ in range(10):
        if page is None or id(page) in visited:
            break
        visited.add(id(page))
        try:
            url = page.url
            if isinstance(url, str):
                url = url[5:] if url.startswith("blob:") else url
                for site in _HOSTS:
                    if belongs_to_site(url, site):
                        return site
            page = page.opener()
        except Exception:
            break
    return None


def make_output_directory(request: RunRequest) -> Path:
    root = request.output_dir.expanduser().resolve()
    destination = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", request.destination.strip())[:35].rstrip(". ") or "출장"
    stamp = datetime.now(KST).strftime("%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    transport = "기차" if request.resolved_transport_mode == "train" else "자차"
    directory = root / f"{request.trip_date}_{transport}_{destination}_{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


class AutomationWorker(threading.Thread):
    """All Playwright operations stay on this worker's single thread."""

    def __init__(self, events: queue.Queue) -> None:
        super().__init__(name="trip-evidence-browser", daemon=True)
        self.events = events
        self.commands: queue.Queue = queue.Queue()
        self.cancelled = threading.Event()
        self.stopping = threading.Event()
        self._context = None
        self._playwright = None
        self._channel = None
        self._current_site: str | None = None
        self._hipass_page_ids: set[int] = set()
        self._receipt_page_ids = {"hipass": self._hipass_page_ids, "korail": set()}
        self._active_request: RunRequest | None = None
        self._skipped_sites: list[str] = []
        self._queued_pdf_hashes: set[str] = set()
        self._pdf_viewer_warnings: set[str] = set()

    def emit(self, event: str, **data) -> None:
        self.events.put({"type": event, **data})

    def log(self, message: str) -> None:
        self.emit("log", message=message)

    def submit(self, request: RunRequest) -> None:
        self.cancelled.clear()
        self.commands.put(("run", request))

    def capture_current(self, page_id: str | None = None) -> None:
        self.commands.put(("capture", page_id))

    def refresh_pages(self) -> None:
        self.commands.put(("refresh", None))

    def capture_external(self, image_path: Path, window_title: str) -> None:
        self.commands.put(("external", (Path(image_path), window_title)))

    def import_pdf(self, path: Path) -> None:
        self.commands.put(("import_pdf", (Path(path), "", False)))

    def finish_receipts(self) -> None:
        self.commands.put(("finish_receipts", None))

    def retry_korail(self) -> None:
        self.commands.put(("retry_korail", None))

    def skip_current(self) -> None:
        self.commands.put(("skip", None))

    def cancel(self) -> None:
        self.cancelled.set()
        self.commands.put(("cancel", None))

    def shutdown(self) -> None:
        self.stopping.set()
        self.cancel()
        self.commands.put(("shutdown", None))

    def _ensure_browser(self, requested: str):
        from playwright.sync_api import sync_playwright

        if self._context and self._channel == requested:
            try:
                if self._context.pages:
                    return self._context
            except Exception:
                pass
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        profile = app_data_dir() / "profiles" / requested
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), channel=requested, headless=False,
                viewport={"width": 1440, "height": 1000}, device_scale_factor=1,
                locale="ko-KR", timezone_id="Asia/Seoul", accept_downloads=True,
                args=["--window-size=1460,1100"],
            )
        except Exception as exc:
            label = "Chrome" if requested == "chrome" else "Edge"
            raise RuntimeError(f"{label} 실행에 실패했습니다. 해당 브라우저 설치 여부를 확인하거나 다른 브라우저를 선택해 주세요. 프로그램이 중복 실행 중이면 먼저 닫아 주세요.") from exc
        self._channel = requested
        self._context.set_default_timeout(10000)
        self._context.set_default_navigation_timeout(30000)
        for page in self._context.pages:
            self._configure_page(page)
        self._context.on("page", self._configure_page)
        self._context.on("response", self._on_context_pdf_response)
        return self._context

    def _configure_page(self, page) -> None:
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", self._on_download)
        if self._current_site in _RECEIPT_SITES and known_page_owner(page) in (None, self._current_site):
            # Print services may use a new host, blob URL or noopener popup.
            # These pages still belong to this controlled receipt session.
            self._receipt_page_ids[self._current_site].add(id(page))

    def _on_context_pdf_response(self, response) -> None:
        try:
            self._on_pdf_response(response.frame.page, response)
        except Exception:
            pass

    def _is_related_page(self, page, site: str) -> bool:
        owner = known_page_owner(page)
        if owner is not None:
            return owner == site
        visited = set()
        current = page
        for _ in range(10):
            if current is None or id(current) in visited:
                return False
            visited.add(id(current))
            try:
                url = current.url
                if isinstance(url, str) and (belongs_to_site(url, site) or (url.startswith("blob:") and belongs_to_site(url[5:], site))):
                    return True
                if site in _RECEIPT_SITES and id(current) in self._receipt_page_ids[site]:
                    return True
                current = current.opener()
            except Exception:
                return False
        return False

    def _staging_pdf(self) -> Path:
        directory = app_data_dir() / "staging"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"download-{uuid.uuid4().hex}.pdf"

    def _queue_pdf_file(self, path: Path, source_url: str) -> None:
        with path.open("rb") as source:
            is_pdf = b"%PDF-" in source.read(1024)
        if not is_pdf:
            path.unlink(missing_ok=True)
            return
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        signature = f"{self._current_site}:" + digest.hexdigest()
        if signature in self._queued_pdf_hashes:
            path.unlink(missing_ok=True)
            return
        self._queued_pdf_hashes.add(signature)
        self.commands.put(("import_pdf", (path, sanitize_source_url(source_url), True, self._current_site)))
        self.log("영수증 PDF를 감지했습니다. 원본과 모든 페이지를 보고서에 저장합니다.")

    def _on_download(self, download) -> None:
        path = None
        try:
            if self._current_site not in _RECEIPT_SITES or not self._is_related_page(download.page, self._current_site):
                return
            path = self._staging_pdf()
            download.save_as(str(path))
            self._queue_pdf_file(path, download.url)
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            self.log("다운로드한 영수증을 자동으로 가져오지 못했습니다. 저장한 PDF를 '영수증 PDF 가져오기'로 선택해 주세요.")

    def _on_pdf_response(self, page, response) -> None:
        path = None
        try:
            if self._current_site not in _RECEIPT_SITES or not self._is_related_page(page, self._current_site):
                return
            if "application/pdf" not in response.headers.get("content-type", "").lower():
                return
            # Read the original response; never repeat an authenticated or
            # one-time print request just to obtain the PDF a second time.
            data = response.body()
            if len(data) > 100 * 1024 * 1024 or b"%PDF-" not in data[:1024]:
                self._pdf_viewer_notice(response.url)
                return
            path = self._staging_pdf()
            path.write_bytes(data)
            self._queue_pdf_file(path, response.url)
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            # Some browser PDF viewers keep the response body unavailable.
            # The explicit file-import action remains available in that case.

    def _pdf_viewer_notice(self, url: str) -> None:
        if url not in self._pdf_viewer_warnings:
            self._pdf_viewer_warnings.add(url)
            self.log("PDF 뷰어로 열린 영수증은 PDF 파일로 저장한 뒤 '영수증 PDF 가져오기'에서 선택해 주세요. 모든 페이지를 저장할 수 있습니다.")

    def _capture_pages(self, site: str):
        pages = []
        try:
            candidates = list(self._context.pages)
        except Exception:
            return pages
        for page in candidates:
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            if closed:
                continue
            allowed = self._is_related_page(page, site)
            if allowed:
                pages.append(page)
        return pages

    def _emit_pages(self, site: str, prepared: PreparedPage) -> None:
        if site == "korail" and prepared.page is None:
            # An ordinary browser is deliberately not attached to Playwright.
            # Do not offer stale tabs from a previous managed-browser request.
            self.emit("pages", pages=[], selected_id="", browserless=True)
            return
        candidates = []
        pages = self._capture_pages(site)
        for page in pages:
            try:
                candidates.append({"id": str(id(page)), "title": page.title(), "url": sanitize_source_url(page.url)})
            except Exception:
                pass
        selected = str(id(prepared.page)) if prepared.page in pages else (candidates[-1]["id"] if candidates else "")
        self.emit("pages", pages=candidates, selected_id=selected)

    def _choose_page(self, site: str, prepared: PreparedPage, page_id: str | None = None):
        pages = self._capture_pages(site)
        if page_id is not None:
            for page in pages:
                if str(id(page)) == page_id:
                    return page
            raise RuntimeError("선택한 창이 닫혔습니다. '창 목록 새로고침' 후 저장할 창을 다시 선택해 주세요.")
        for page in reversed(pages):
            try:
                if page.evaluate("document.hasFocus()"):
                    return page
            except Exception:
                continue
        if prepared.page in pages:
            return prepared.page
        if pages:
            return pages[-1]
        raise RuntimeError("해당 사이트의 조회 창이 없습니다. 브라우저에서 조회 화면을 연 뒤 다시 저장해 주세요.")

    def _capture(self, site: str, prepared: PreparedPage, directory: Path, manual: bool, page_id: str | None = None) -> CaptureRecord:
        if site == "korail" and prepared.page is None:
            raise RuntimeError("일반 브라우저 영수증은 ‘일반 브라우저 영수증 저장’ 또는 ‘영수증 PDF 가져오기’를 사용해 주세요.")
        page = self._choose_page(site, prepared, page_id) if manual else prepared.page
        if page.is_closed():
            raise RuntimeError("캡처할 창이 닫혔습니다. 조회 창을 다시 열어 주세요.")
        if not self._is_related_page(page, site):
            raise RuntimeError("대상 사이트의 화면을 선택해 주세요.")
        if site == "hipass" and re.search(r"/(?:comm/(?:lginpg|anyidLogin)|cmmn/firstVist)", page.url, re.I):
            raise RuntimeError("아직 하이패스 로그인 화면입니다. 로그인 후 사용내역이나 영수증 화면을 준비해 주세요.")
        for frame in page.frames:
            for password_field in frame.locator('input[type="password"]').all():
                if password_field.is_visible():
                    raise RuntimeError("비밀번호 입력 화면은 저장하지 않습니다. 로그인·인증을 완료하고 조회 결과 화면을 선택해 주세요.")
        if site in _RECEIPT_SITES and (urlsplit(page.url).path.lower().endswith(".pdf") or page.locator('embed[type="application/pdf"], object[type="application/pdf"]').count() > 0):
            raise RuntimeError("PDF 영수증은 파일로 저장한 뒤 '영수증 PDF 가져오기'로 선택해 주세요. 화면 일부 대신 모든 페이지를 보고서에 넣습니다.")
        verified_details = {}
        capture_target = None
        if site == "korail":
            from .korail import inspect_korail_receipt, korail_receipt_target
            if self._active_request is None:
                raise RuntimeError("출장 조건을 확인하지 못했습니다. 코레일 작업을 다시 시작해 주세요.")
            tracked_popup = id(page) in self._receipt_page_ids[site]
            verified_details = inspect_korail_receipt(page, self._active_request, trusted_popup=tracked_popup)
            capture_target = korail_receipt_target(page, trusted_popup=tracked_popup)
        page.bring_to_front()
        try:
            page.evaluate("document.fonts.ready")
        except Exception:
            pass
        image = directory / f"{site}_{datetime.now(KST).strftime('%H%M%S_%f')}.png"
        try:
            if capture_target is not None:
                capture_target.screenshot(path=str(image), animations="disabled", timeout=15000)
            else:
                page.screenshot(path=str(image), full_page=site != "naver", animations="disabled", timeout=15000)
        except Exception as exc:
            image.unlink(missing_ok=True)
            raise RuntimeError("화면 저장에 실패했습니다. 조회 화면이 열려 있는지 확인하고 다시 저장해 주세요.") from exc
        details = dict(prepared.details)
        details.update(verified_details)
        details["저장 방식"] = "사용자가 준비한 화면" if manual else "조회 조건을 확인한 자동 캡처"
        source_url = page.url
        if source_url == "about:blank":
            try:
                source_url = page.opener().url
            except Exception:
                pass
        record = CaptureRecord(site=site, title=SITE_LABELS[site], image_path=image,
                               url=sanitize_source_url(source_url),
                               captured_at=datetime.now(KST).isoformat(timespec="seconds"),
                               details=details, manual=manual)
        self.log(f"{SITE_LABELS[site]} 원본 화면 저장 완료: {image.name}")
        self.emit("captured", path=str(image), site=site)
        return record

    def _manual_capture(self, site: str, prepared: PreparedPage, directory: Path, on_saved=None):
        collected: list[CaptureRecord] = []
        browserless = site == "korail" and prepared.page is None

        def remember(records):
            collected.extend(records if isinstance(records, list) else [records])
            if on_saved:
                try:
                    on_saved(collected)
                except Exception:
                    self.log("중간 보고서를 저장하지 못했습니다. 수집 완료 시 다시 저장합니다.")

        def wait_for_more(message: str = ""):
            self.emit("state", value="waiting")
            instruction = message or prepared.message or "브라우저에서 원하는 조회 화면을 준비한 뒤 '현재 화면 저장'을 눌러 주세요."
            if site == "korail" and collected:
                instruction = message or (f"코레일 영수증 화면·페이지 {len(collected)}개를 저장했습니다. "
                                          "왕복 등 다음 영수증을 계속 추가하거나 ‘코레일 수집 완료’를 눌러 주세요.")
            self.emit("manual", site=site, message=instruction, browserless=browserless, collected_count=len(collected))
            self._emit_pages(site, prepared)

        if site == "korail" and prepared.ready:
            try:
                remember(self._capture(site, prepared, directory, manual=False))
            except Exception as exc:
                prepared.message = str(exc)
        wait_for_more()
        while not self.cancelled.is_set() and not self.stopping.is_set():
            try:
                command, data = self.commands.get(timeout=0.15)
            except queue.Empty:
                try:
                    if prepared.page is not None and not prepared.page.is_closed():
                        prepared.page.wait_for_timeout(100)
                except Exception:
                    pass
                continue
            if command in ("cancel", "shutdown"):
                return collected or None
            if command in ("skip", "finish_receipts"):
                if command == "finish_receipts" and site != "korail":
                    continue
                if command == "finish_receipts" and browserless and not collected:
                    wait_for_more("저장된 코레일 영수증이 아직 없습니다. ‘영수증 PDF 가져오기’ 또는 ‘일반 브라우저 영수증 저장’으로 자료를 추가한 뒤 수집을 완료해 주세요.")
                    continue
                if command == "skip":
                    if not collected and site not in self._skipped_sites:
                        self._skipped_sites.append(site)
                    self.log(f"{SITE_LABELS[site]} 항목을 건너뛰었습니다. 선택한 다음 항목이 있으면 계속 진행합니다.")
                elif not collected:
                    self.log(f"{SITE_LABELS[site]} 수집 완료를 선택했습니다. 저장된 영수증은 없습니다.")
                return collected or None
            if command == "refresh":
                self._emit_pages(site, prepared)
            if command == "retry_korail" and site == "korail":
                if browserless:
                    wait_for_more(_KORAIL_MANUAL_MESSAGE)
                    continue
                try:
                    from .korail import prepare_korail
                    self.emit("state", value="running")
                    self.emit("status", site=site, message="로그인 상태와 이용내역 조회 화면을 확인합니다.")
                    prepared = prepare_korail(self._context, self._active_request, self.log)
                    self._receipt_page_ids[site].add(id(prepared.page))
                    if prepared.ready:
                        remember(self._capture(site, prepared, directory, manual=False))
                    wait_for_more()
                except Exception:
                    wait_for_more("이용내역 화면을 자동으로 준비하지 못했습니다. 브라우저에서 ‘이용내역/영수증 조회’를 직접 열거나 PDF·출력창 저장을 이용하세요.")
            if command == "import_pdf":
                path, source_url, owned = data[:3]
                source_site = data[3] if len(data) > 3 else site
                if source_site is not None and source_site != site:
                    self.log("다른 사이트 단계에서 받은 PDF입니다. 해당 단계에서 파일을 직접 가져와 주세요.")
                    continue
                imported = False
                try:
                    if site not in _RECEIPT_SITES:
                        raise ValueError("영수증 PDF는 하이패스·코레일 단계에서 가져올 수 있습니다.")
                    self.emit("state", value="running")
                    self.emit("status", site=site, message="PDF 원본과 모든 페이지를 저장하고 있습니다.")
                    from .pdf_receipts import import_receipt_pdf
                    records = import_receipt_pdf(path, directory, source_url=source_url, site=site)
                    imported = True
                    for record in records:
                        self.emit("captured", path=str(record.image_path), site=site)
                    self.log(f"영수증 PDF 원본과 {len(records)}개 페이지를 저장했습니다.")
                    if site == "korail":
                        remember(records)
                        wait_for_more()
                    else:
                        return records
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "PDF를 읽지 못했습니다. PDF로 저장한 파일을 다시 선택해 주세요."
                    if owned and path.is_file() and path.resolve().parent == (app_data_dir() / "staging").resolve():
                        failed = directory / f"미처리_영수증_{uuid.uuid4().hex[:8]}.pdf"
                        try:
                            shutil.copyfile(path, failed)
                            path.unlink()
                            message += f" 원본은 결과 폴더의 {failed.name}에 보관했습니다."
                        except OSError:
                            message += " 다운로드한 원본 PDF는 앱 임시 저장 폴더에 보관했습니다."
                    wait_for_more(message)
                finally:
                    if owned and imported:
                        staging = (app_data_dir() / "staging").resolve()
                        if path.resolve().parent == staging:
                            path.unlink(missing_ok=True)
            if command == "external":
                try:
                    if site not in _RECEIPT_SITES:
                        raise RuntimeError("출력창 저장은 하이패스·코레일 단계에서 사용할 수 있습니다.")
                    path, title = data
                    staging = (app_data_dir() / "staging").resolve()
                    path = path.resolve()
                    if path.parent != staging or path.suffix.lower() != ".png" or not path.is_file():
                        raise RuntimeError("캡처 파일을 찾지 못했습니다. 영수증 창을 다시 저장해 주세요.")
                    from PIL import Image
                    with Image.open(path) as screenshot:
                        if screenshot.format != "PNG" or min(screenshot.size) < 100:
                            raise RuntimeError("캡처 이미지가 올바르지 않습니다. 창을 다시 저장해 주세요.")
                        screenshot.verify()
                    saved = directory / f"{site}_window_{datetime.now(KST).strftime('%H%M%S_%f')}_{uuid.uuid4().hex[:6]}.png"
                    shutil.copyfile(path, saved)
                    path.unlink()
                    record = CaptureRecord(site, SITE_LABELS[site], saved, _RECEIPT_URLS[site],
                                           datetime.now(KST).isoformat(timespec="seconds"), {
                                               "저장 방식": "사용자가 선택한 영수증·PDF 뷰어·출력 프로그램 전체 창 캡처",
                                               "선택한 창 제목": str(title),
                                               "조회 확인": "사용자가 직접 로그인·조회한 화면입니다. 정확한 화면 주소와 날짜는 이미지에서 확인해 주세요.",
                                           }, manual=True)
                    self.emit("captured", path=str(saved), site=site)
                    self.log("선택한 영수증·출력 창을 원본 화면 이미지로 저장했습니다.")
                    if site == "korail":
                        remember(record)
                        wait_for_more()
                    else:
                        return record
                except Exception:
                    wait_for_more("출력창 캡처를 가져오지 못했습니다. 영수증 창을 다시 선택해 저장해 주세요.")
            if command == "capture":
                try:
                    record = self._capture(site, prepared, directory, manual=True, page_id=data)
                    if site == "korail":
                        remember(record)
                        wait_for_more()
                    else:
                        return record
                except Exception as exc:
                    wait_for_more(str(exc))
        return collected or None

    def _run_request(self, request: RunRequest) -> None:
        from .sites import prepare_hipass, prepare_naver, prepare_opinet
        from .korail import prepare_korail

        request.validate()
        self._active_request = request
        self._skipped_sites.clear()
        transport_label = "기차 이용" if request.resolved_transport_mode == "train" else "자차 이용"
        self.log(f"{transport_label} 출장 자료를 준비합니다.")
        self._queued_pdf_hashes.clear()
        self._pdf_viewer_warnings.clear()
        self.emit("state", value="running")
        directory = make_output_directory(request)
        records: list[CaptureRecord] = []
        notices: list[str] = []
        try:
            browserless_korail = all(site == "korail" for site in request.sites)
            context = None if browserless_korail else self._ensure_browser(request.browser)
            password = None
            if "hipass" in request.sites and request.use_saved_password and request.hipass_id:
                password = WindowsCredentialStore().get("hipass:" + request.hipass_id)
            for step_index, site in enumerate(request.sites, start=1):
                if self.cancelled.is_set() or self.stopping.is_set():
                    notices.append("사용자가 작업을 중지했습니다. 이미 저장한 화면만 포함합니다.")
                    break
                self.emit("state", value="running")
                self._current_site = site
                next_site = request.sites[step_index] if step_index < len(request.sites) else ""
                self.emit("step", site=site, index=step_index, total=len(request.sites), next_site=next_site, sites=list(request.sites))
                self.emit("status", site=site, message=f"{SITE_LABELS[site]} 화면 준비 중")
                try:
                    if site == "hipass":
                        prepared = prepare_hipass(context, request, self.log, password=password)
                        self._hipass_page_ids.add(id(prepared.page))
                        password = None
                    elif site == "korail":
                        if browserless_korail:
                            prepared = _ordinary_korail_page(request)
                            self.log("코레일은 일반 브라우저에서 로그인·조회한 영수증을 PDF 또는 창 캡처로 수집합니다.")
                        else:
                            prepared = prepare_korail(context, request, self.log)
                            self._receipt_page_ids[site].add(id(prepared.page))
                    else:
                        adapter = prepare_naver if site == "naver" else prepare_opinet
                        prepared = adapter(context, request, self.log)
                    if self.cancelled.is_set() or self.stopping.is_set():
                        notices.append(f"{SITE_LABELS[site]} 준비 도중 중지했습니다.")
                        break
                    if site == "korail":
                        record = self._manual_capture(site, prepared, directory,
                            on_saved=lambda saved: write_report(request, records + list(saved), directory, notices))
                    elif prepared.ready:
                        try:
                            record = self._capture(site, prepared, directory, manual=False)
                        except Exception as exc:
                            prepared.ready = False
                            prepared.message = str(exc)
                            record = self._manual_capture(site, prepared, directory)
                    else:
                        record = self._manual_capture(site, prepared, directory)
                    if record:
                        records.extend(record if isinstance(record, list) else [record])
                        # Save after every successful capture, so an interruption cannot lose the report.
                        write_report(request, records, directory, notices)
                    else:
                        notices.append(f"{SITE_LABELS[site]}: 사용자가 건너뛰었습니다." if site in self._skipped_sites
                                       else f"{SITE_LABELS[site]}: 화면이 저장되지 않았습니다.")
                except Exception as exc:
                    # Never include Playwright call arguments (which may contain a password) in logs.
                    notices.append(f"{SITE_LABELS[site]} 처리 중 오류가 발생했습니다. 저장된 화면을 확인해 주세요.")
                    self.log(f"{SITE_LABELS[site]} 처리 오류 ({type(exc).__name__}). 다음 항목으로 진행합니다.")
            if self.cancelled.is_set() and not any("중지" in notice for notice in notices):
                notices.append("사용자가 작업을 중지했습니다. 저장 완료된 화면만 포함합니다.")
        except Exception as exc:
            notices.append(str(exc) if isinstance(exc, RuntimeError) else "브라우저 준비 중 오류가 발생했습니다.")
            self.emit("error", message=notices[-1])
        finally:
            self._current_site = None
            self._active_request = None
            report = write_report(request, records, directory, notices)
            self.emit("done", report_path=str(report), output_dir=str(directory), count=len(records),
                      site_count=len({record.site for record in records}), skipped_sites=list(self._skipped_sites),
                      transport_mode=request.resolved_transport_mode)
            self.emit("state", value="idle")

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    command, data = self.commands.get(timeout=0.25)
                except queue.Empty:
                    continue
                if command == "shutdown":
                    break
                if command != "run":
                    continue
                try:
                    self._run_request(data)
                except Exception as exc:
                    self.emit("error", message=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "작업을 완료하지 못했습니다. 출력 폴더와 브라우저 상태를 확인해 주세요.")
                    self.emit("state", value="idle")
        finally:
            if self._context:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
