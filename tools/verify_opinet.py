"""Read-only live check against a public sample, using no saved user profile."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from trip_capture.models import RunRequest, CaptureRecord
from trip_capture.sites import prepare_opinet
from trip_capture.report import write_report
from datetime import datetime, timezone, timedelta


def main():
    output = Path(__file__).resolve().parents[1] / "artifacts" / "verification" / "opinet"
    output.mkdir(parents=True, exist_ok=True)
    request = RunRequest("2026-09-30", "행정안전부", "무안국제공항", sites=("opinet",), output_dir=output)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", timezone_id="Asia/Seoul")
        context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.dismiss()))
        result = prepare_opinet(context, request, print)
        if not result.ready:
            try:
                result.page.get_by_role("cell", name="2026년09월30일", exact=True).wait_for(state="visible", timeout=12000)
            except Exception:
                pass
        (output / "tables.json").write_text(json.dumps(result.page.locator("table").evaluate_all("tables => tables.map(t => ({id:t.id, parentId:t.parentElement.id, html:t.outerHTML}))"), ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"ready": result.ready, "message": result.message, "url": result.page.url, "details": result.details}
        print(json.dumps(summary, ensure_ascii=True))
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "dom.txt").write_text(result.page.locator("body").inner_text(), encoding="utf-8")
        result.page.screenshot(path=str(output / "screen.png"), full_page=True)
        if result.ready:
            record = CaptureRecord("opinet", "오피넷 일일 유가 · 공개 예시 검증", output / "screen.png", result.page.url,
                                   datetime.now(timezone(timedelta(hours=9))).isoformat(), result.details)
            write_report(request, [record], output)
        browser.close()
        return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
