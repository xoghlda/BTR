"""Headless, local-only Korail workflow integration check using fictional data.

Every browser request is intercepted. No account, existing browser profile,
desktop window, real ticket, payment, or live Korail service is accessed.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import queue
import re
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from trip_capture.automation import AutomationWorker
from trip_capture.models import PreparedPage, RunRequest


FIXTURE_ROOT = "https://www.korail.com/__codex_local_fixture__/"


def receipt_html(day: str, origin: str, destination: str, label: str) -> str:
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>개발용 가상자료 {label}</title><style>
body {{ margin: 32px; font: 20px 'Malgun Gothic', sans-serif; color: #17243b; background: #f5f7fb; }}
main {{ max-width: 750px; margin: auto; padding: 26px; border: 2px solid #284b77; background: white; }}
.fixture {{ color: #a03508; background: #fff0d1; padding: 16px; font-weight: bold; }}
h1 {{ border-bottom: 2px solid #284b77; padding-bottom: 15px; }} p {{ margin: 14px 0; }}
</style><main><div class="fixture">개발용 가상자료 · 실제 승차권/거래 아님 · 정산 제출 금지</div>
<h1>운임영수증</h1><p>한국철도공사 KORAIL · 개발 검증용 가상자료 {label}</p>
<p>승차일: {day}</p><p>출발역: {origin}</p><p>도착역: {destination}</p>
<p>열차: KTX 개발용 가상 열차</p><p>결제방법: 테스트 결제</p>
<p>승인번호: FIXTURE-{label}</p><p>결제금액: 18,500원</p>
<p class="fixture">이 화면은 로컬 테스트 HTML이며 실제 코레일 응답이 아닙니다.</p></main></html>"""


def fictional_pdf(page_count: int = 3) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Count {page_count} /Kids [" + " ".join(f"{4 + index * 2} 0 R" for index in range(page_count)) + "] >>").encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(page_count):
        page_id = 4 + index * 2
        content = (
            "BT /F1 16 Tf 24 385 Td (DEVELOPMENT TEST FIXTURE ONLY) Tj ET\n"
            "BT /F1 11 Tf 24 360 Td (FICTIONAL DATA - NOT A VALID RECEIPT) Tj ET\n"
            f"BT /F1 18 Tf 24 320 Td (KORAIL PDF PAGE {index + 1} OF {page_count}) Tj ET\n"
            f"{0.2 + index * 0.2:.2f} 0.3 0.7 rg 24 240 250 45 re f\n"
            "BT /F1 11 Tf 24 200 Td (No account or payment information used.) Tj ET\n"
        ).encode("ascii")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 420] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(),
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
        ])
    result = io.BytesIO()
    result.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(result.tell())
        result.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = result.tell()
    result.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.write(f"{offset:010d} 00000 n \n".encode())
    result.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return result.getvalue()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def fare_rows(content: str) -> list[list[str]]:
    rows = []
    for table in re.findall(r'<table class="fares".*?</table>', content, re.S):
        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            columns = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if columns:
                rows.append([html.unescape(re.sub(r"<[^>]+>", "", value)) for value in columns])
    return rows


def main() -> int:
    output = Path(__file__).resolve().parents[1] / "artifacts" / "verification" / "korail_v12"
    output.mkdir(parents=True, exist_ok=True)
    source = output / "개발용_가상자료_3pages.pdf"
    source.write_bytes(fictional_pdf())
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    events: queue.Queue = queue.Queue()
    worker = AutomationWorker(events)
    intercepted_urls: list[str] = []
    emitted_events: list[dict] = []
    fixture_pages: dict[str, str] = {}
    result = {"fixture_only": True, "uses_existing_profile": False, "success": False}

    def isolated_browser(self, requested):
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 850}, locale="ko-KR", timezone_id="Asia/Seoul")
        context.set_default_timeout(5000)
        context.set_default_navigation_timeout(5000)
        def intercept(route):
            url = route.request.url
            intercepted_urls.append(url)
            if url == FIXTURE_ROOT + "outbound":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=receipt_html("2026-10-20", "오송", "서울", "OUTBOUND"))
            elif url == FIXTURE_ROOT + "return":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=receipt_html("2026-10-21", "용산", "오송", "RETURN"))
            else:
                route.abort()
        context.route("**/*", intercept)
        context.on("page", self._configure_page)
        self._context = context
        self._channel = requested
        def stop():
            try:
                browser.close()
            finally:
                playwright.stop()
        self._playwright = SimpleNamespace(stop=stop)
        return context

    def prepare_fixture(context, request, log):
        pages = []
        for name in ("outbound", "return"):
            page = context.new_page()
            page.goto(FIXTURE_ROOT + name, wait_until="domcontentloaded")
            fixture_pages[name] = str(id(page))
            pages.append(page)
        log("개발용 가상자료: 두 개의 로컬 왕복 영수증 화면을 준비했습니다.")
        return PreparedPage(pages[0], ready=False, message="개발용 가상자료: 화면 두 개와 PDF를 수집합니다.", details={"fixture": "개발용 가상자료 · 실제 거래 아님"})

    worker._ensure_browser = MethodType(isolated_browser, worker)
    request = RunRequest(
        trip_date="2026-10-20", trip_end_date="2026-10-21", origin="개발용 가상 출발지", destination="개발용 가상 출장지",
        sites=("korail",), output_dir=output, rail_origin="오송", rail_destination="서울",
        rail_return_origin="용산", rail_return_destination="오송",
        department="개발용 가상기관", position="개발 검증용", traveler_name="가상인물", trip_purpose="로컬 통합검증 · 실제 출장 아님",
    )
    step = 0
    done = None
    try:
        with patch("trip_capture.korail.prepare_korail", side_effect=prepare_fixture):
            worker.start()
            worker.submit(request)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                emitted_events.append(event)
                if event["type"] == "pages":
                    if step == 0:
                        worker.capture_current(fixture_pages["outbound"])
                    elif step == 1:
                        worker.capture_current(fixture_pages["return"])
                    elif step == 2:
                        worker.import_pdf(source)
                    elif step == 3:
                        worker.finish_receipts()
                    step += 1
                if event["type"] == "done":
                    done = event
                    break
            check(done is not None, "Worker did not finish within 60 seconds")
            check(step == 4, f"Expected two captures, PDF import, finish; got {step} steps")
            check(done["count"] == 5 and done["site_count"] == 1, f"Unexpected capture count: {done}")
            directory = Path(done["output_dir"])
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            check(manifest["status"] == "complete", "Report status is not complete")
            captures = manifest["captures"]
            check(len(captures) == 5 and all(item["site"] == "korail" for item in captures), "Not all 5 records were classified as Korail")
            for index, expected in enumerate((
                {"승차일": "2026-10-20", "출발역": "오송", "도착역": "서울", "rail_direction": "가는편"},
                {"승차일": "2026-10-21", "출발역": "용산", "도착역": "오송", "rail_direction": "오는편"},
            )):
                for key, value in expected.items():
                    check(captures[index]["details"].get(key) == value, f"Receipt {index + 1} missing confirmed {key}={value}")
            check(len(manifest["attachments"]) == 1, "Expected one original PDF attachment")
            original = directory / manifest["attachments"][0]["path"]
            check(hashlib.sha256(original.read_bytes()).hexdigest() == source_hash, "Original PDF bytes changed")
            check(hashlib.sha256(source.read_bytes()).hexdigest() == source_hash, "Source PDF was changed")
            pdf_records = [item for item in captures if item.get("kind") == "pdf_page"]
            check(len(pdf_records) == 3, "Not all three PDF pages were imported")
            check({item["details"]["PDF 페이지"] for item in pdf_records} == {"1/3", "2/3", "3/3"}, "PDF page metadata is incomplete")
            for capture in captures:
                image = directory / capture["image_path"]
                check(image.is_file() and image.stat().st_size > 1000, "Screenshot/rendered page was not saved")
                check(hashlib.sha256(image.read_bytes()).hexdigest() == capture["image_sha256"], "Image checksum mismatch")
            rail = directory / "rail_settlement.html"
            settlement = rail.read_text(encoding="utf-8")
            rows = fare_rows(settlement)
            check(len(rows) == 5, "Settlement did not include all five evidence pages")
            check(rows[0][2].startswith("오송") and rows[0][3].startswith("서울"), "Outbound settlement stations are wrong")
            check(rows[1][2].startswith("용산") and rows[1][3].startswith("오송"), "Return settlement stations are wrong")
            check(settlement.count('class="sheet"') == 3, "Expected three A4 settlement sheets")
            check(all(url.startswith(FIXTURE_ROOT) for url in intercepted_urls), "Browser requested a non-fixture URL")
            check(not any(event["type"] == "error" for event in emitted_events), "Worker emitted a runtime error")
            result.update(success=True, output_dir=str(directory), report=str(done["report_path"]), rail_settlement=str(rail),
                          capture_count=5, pdf_pages=3, original_pdf_sha256=source_hash, round_trip_verified=True,
                          source_urls_intercepted=intercepted_urls, example_image=str(directory / captures[0]["image_path"]))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        worker.shutdown()
        if worker.ident is not None:
            worker.join(timeout=10)
        if worker.is_alive():
            result.update(success=False, shutdown_error="Worker did not stop")
        (output / "events.json").write_text(json.dumps(emitted_events, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
