"""Reproduce receipt popups and PDF downloads using synthetic localhost data.

No account, user PDF, browser profile or Downloads folder is accessed.  The
hipass origin below is fulfilled from our local fixture server before network
access; only the application's ownership check sees that origin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from trip_capture.automation import AutomationWorker
from trip_capture.models import PreparedPage, RunRequest


SEED_URL = "https://www.hipass.co.kr/__codex_receipt_fixture__"
TITLES = {"popup-ip": "영수증 개발검증 IP 창", "popup-blank": "영수증 개발검증 빈 창",
          "popup-blob": "영수증 개발검증 Blob 창"}
RECEIPT_HTML = """<!doctype html><html lang="ko"><meta charset="utf-8">
<title>영수증 개발검증 IP 창</title><style>
body{font:20px sans-serif;margin:45px;max-width:900px}table{border-collapse:collapse}
th,td{border:1px solid #333;padding:15px}p{line-height:1.6}
</style><h1>영수증 개발 검증용 가상 화면</h1>
<p>이 화면은 로컬 테스트 자료입니다. 실제 계정·차량·통행 내역이 아닙니다.</p>
<table><tr><th>검증 날짜</th><td>2026-09-30</td></tr>
<tr><th>검증 항목</th><td>별도 출력 창의 원본 화면 저장</td></tr></table>
</html>"""


def synthetic_pdf() -> bytes:
    """A deterministic, three-page PDF with ASCII fixture labels only."""
    streams = []
    for number in (1, 2, 3):
        streams.append((f"BT /F1 19 Tf 45 770 Td (SYNTHETIC RECEIPT - PAGE {number}/3) Tj "
                        "0 -42 Td /F1 12 Tf (DEVELOPMENT FIXTURE ONLY) Tj "
                        "0 -28 Td (Not an actual account, vehicle or toll record.) Tj "
                        f"0 -70 Td (Distinct test page: {number}) Tj ET").encode("ascii"))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R 7 0 R] /Count 3 >>",
    ]
    for index, stream in enumerate(streams):
        content_id = 4 + index * 2
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                        f"/Resources << /Font << /F1 9 0 R >> >> /Contents {content_id} 0 R >>").encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    startxref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(data)


def seed_html(base: str) -> str:
    body = json.dumps(RECEIPT_HTML, ensure_ascii=False)
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8"><title>로컬 영수증 검증 시작</title>
    <h1>개인정보 없는 로컬 영수증 재현 화면</h1>
    <button id="popup-ip" onclick="window.open('{base}/receipt-window','receipt-ip')">IP 출력창</button>
    <button id="popup-blank" onclick="openBlank()">빈 주소 출력창</button>
    <button id="popup-blob" onclick="openBlob()">Blob 출력창</button>
    <a id="download" href="{base}/receipt.pdf">가상 영수증 PDF 다운로드</a>
    <a id="inline" href="{base}/inline.pdf" target="_blank">PDF 뷰어 열기</a>
    <script>const fixture={body};
    function openBlank(){{let p=window.open('about:blank','receipt-blank');p.document.write(fixture);
      p.document.close();p.document.title='영수증 개발검증 빈 창';}}
    function openBlob(){{let html=fixture.replace('영수증 개발검증 IP 창','영수증 개발검증 Blob 창');
      window.open(URL.createObjectURL(new Blob([html],{{type:'text/html'}})),'receipt-blob');}}
    </script></html>"""


@contextmanager
def local_fixture_server(pdf: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route in ("/receipt.pdf", "/inline.pdf"):
                body, content_type = pdf, "application/pdf"
            elif route == "/receipt-window":
                body, content_type = RECEIPT_HTML.encode("utf-8"), "text/html; charset=utf-8"
            elif route == "/":
                base = f"http://127.0.0.1:{self.server.server_port}"
                body, content_type = seed_html(base).encode("utf-8"), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if route in ("/receipt.pdf", "/inline.pdf"):
                disposition = "attachment" if route == "/receipt.pdf" else "inline"
                self.send_header("Content-Disposition", f'{disposition}; filename="synthetic-receipt.pdf"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="receipt-fixture-http", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def install_fixture_route(context, base: str) -> None:
    # Explicitly fulfill only the synthetic URL, without visiting the service.
    with urlopen(base + "/", timeout=3) as response:
        html = response.read()
    context.route(SEED_URL, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))


def open_fixture_popup(seed, case: str):
    with seed.expect_popup(timeout=10_000) as opened:
        seed.locator("#" + case).click()
    popup = opened.value
    popup.locator("h1").wait_for(timeout=10_000)
    return popup


def probe(output: Path, base: str, pdf: bytes, channel: str) -> dict:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", accept_downloads=True)
        context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.dismiss()))
        install_fixture_route(context, base)
        seed = context.new_page()
        seed.goto(SEED_URL)
        worker = AutomationWorker(queue.Queue())
        worker._context = context
        ownership = []
        for case in TITLES:
            popup = open_fixture_popup(seed, case)
            included = popup in worker._capture_pages("hipass")
            popup.screenshot(path=str(output / f"{case}-probe.png"), full_page=True)
            ownership.append({"case": case, "url": popup.url, "opener_matches_seed": popup.opener() is seed,
                              "included_by_worker": included})
        with seed.expect_download(timeout=10_000) as incoming:
            seed.locator("#download").click()
        downloaded = output / "probe-downloaded.pdf"
        incoming.value.save_as(downloaded)
        assert downloaded.read_bytes() == pdf, "Download bytes changed"
        context.close()
        browser.close()
    return {"mode": "probe", "popup_ownership": ownership, "download_preserved": True}


def integrated_case(case: str, output: Path, base: str, fixture_pdf: Path, channel: str) -> dict:
    if not hasattr(AutomationWorker, "import_pdf") or not hasattr(AutomationWorker, "_configure_page"):
        raise RuntimeError("Worker PDF integration API is not ready; use --mode probe first.")
    events: queue.Queue = queue.Queue()
    shared: dict[str, Any] = {}

    class FixtureWorker(AutomationWorker):
        def _ensure_browser(self, requested):
            self._playwright = sync_playwright().start()
            self._test_browser = self._playwright.chromium.launch(channel=requested, headless=True)
            self._context = self._test_browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", accept_downloads=True)
            install_fixture_route(self._context, base)
            self._context.on("page", self._configure_page)
            if hasattr(self, "_on_context_pdf_response"):
                self._context.on("response", self._on_context_pdf_response)
            if case == "inline":
                shared["pdf_response_probe"] = []

                def observe_pdf(response):
                    if "application/pdf" not in response.headers.get("content-type", ""):
                        return
                    info = {"url": response.url}
                    try:
                        data = response.body()
                        info["response_bytes"] = len(data)
                        info["is_pdf"] = b"%PDF-" in data[:1024]
                        info["matches_fixture"] = data == fixture_pdf.read_bytes()
                        info["body_prefix"] = data[:100].decode("ascii", errors="replace")
                    except Exception as error:
                        info["body_error"] = type(error).__name__ + ": " + str(error)
                    shared["pdf_response_probe"].append(info)

                self._context.on("response", observe_pdf)
            # A preexisting unrelated page must not enter the receipt session.
            unrelated = self._context.new_page()
            unrelated.goto(base + "/receipt-window?unrelated=1")
            shared["unrelated_id"] = str(id(unrelated))
            return self._context

    def prepare_fixture(context, request, log, password=None):
        seed = context.new_page()
        seed.goto(SEED_URL)
        if case in TITLES:
            child = open_fixture_popup(seed, case)
            shared["child_id"] = str(id(child))
        elif case == "download":
            seed.locator("#download").click()
        elif case == "inline":
            seed.locator("#inline").click()
        return PreparedPage(seed, False, "개발용 가상 영수증의 저장을 확인합니다.", {"trip_date": request.trip_date})

    worker = FixtureWorker(events)
    requested = False
    fallback_used = False
    done = None
    log_events = []
    with patch("trip_capture.automation.app_data_dir", return_value=output / "private-test-state"), \
            patch("trip_capture.sites.prepare_hipass", side_effect=prepare_fixture):
        worker.start()
        try:
            worker.submit(RunRequest("2026-09-30", "가상 출발지", "가상 도착지", sites=("hipass",), output_dir=output / case, browser=channel))
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                probe_data = shared.get("pdf_response_probe", [])
                if case == "inline" and not requested and probe_data and all(not item.get("is_pdf") for item in probe_data):
                    # Chrome may expose its viewer HTML instead of PDF bytes.
                    # Reproduce the explicit user-selected file-import fallback;
                    # never re-request the source URL to retrieve the PDF.
                    worker.import_pdf(fixture_pdf)
                    requested = True
                    fallback_used = True
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    continue
                log_events.append(event)
                if event["type"] == "pages" and not requested:
                    page_ids = {page["id"] for page in event["pages"]}
                    if case in TITLES:
                        assert shared.get("unrelated_id") not in page_ids, "Unrelated IP page was listed"
                        assert shared.get("child_id") in page_ids, f"{case} was missing from the capture list"
                        worker.capture_current(shared["child_id"])
                        requested = True
                    elif case == "import":
                        worker.import_pdf(fixture_pdf)
                        requested = True
                if event["type"] == "done":
                    done = event
                    break
            probe_data = shared.get("pdf_response_probe", [])
            assert done is not None, json.dumps({"error": "Worker did not finish the fixture case", "case": case,
                                                "probe": probe_data, "events": log_events}, ensure_ascii=False)
        finally:
            worker.shutdown()
            worker.join(timeout=12)
            assert not worker.is_alive(), "Worker failed to close its fixture browser"
    expected = 1 if case in TITLES else 3
    assert done["count"] == expected, json.dumps({"case": case, "done": done, "events": log_events}, ensure_ascii=False)
    directory = Path(done["output_dir"])
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["captures"]) == expected
    if case not in TITLES:
        originals = list(directory.glob("*.pdf"))
        images = list(directory.glob("*.png"))
        assert len(originals) == 1 and originals[0].read_bytes() == fixture_pdf.read_bytes(), "Original PDF not preserved"
        assert len(images) == 3 and len({hashlib.sha256(image.read_bytes()).hexdigest() for image in images}) == 3, "All distinct PDF pages were not rendered"
    return {"case": case, "passed": True, "count": done["count"], "output_dir": str(directory), "events": log_events,
            "pdf_response_probe": shared.get("pdf_response_probe"),
            "automatic": case in ("download", "inline") and not fallback_used,
            "fallback": "file_import" if fallback_used else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "integration"), default="integration")
    parser.add_argument("--channel", choices=("chrome", "msedge"), default="chrome")
    parser.add_argument("--cases", nargs="+", choices=tuple(TITLES) + ("download", "import", "inline"), default=list(TITLES) + ["download", "import", "inline"])
    args = parser.parse_args()
    output = ROOT / "artifacts" / "verification" / "receipt_pdf"
    output.mkdir(parents=True, exist_ok=True)
    pdf = synthetic_pdf()
    fixture_pdf = output / "synthetic-three-page-receipt.pdf"
    fixture_pdf.write_bytes(pdf)
    with local_fixture_server(pdf) as base:
        if args.mode == "probe":
            result = probe(output, base, pdf, args.channel)
        else:
            checks = []
            for case in args.cases:
                try:
                    check = integrated_case(case, output, base, fixture_pdf, args.channel)
                    print(f"PASS {case}: {check['count']} saved pages", flush=True)
                except Exception as error:
                    check = {"case": case, "passed": False, "error": str(error)}
                    print(f"FAIL {case}: {error}", flush=True)
                checks.append(check)
                (output / f"case-{case}-result.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"mode": "integration", "results": checks}
    destination = output / f"{args.mode}-result.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if args.mode == "integration" and not all(check["passed"] for check in result["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
