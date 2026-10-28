"""Developer smoke check against the public Naver route screen."""

from __future__ import annotations

import json
import sys
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from trip_capture.models import RunRequest
from trip_capture.sites import prepare_naver


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="행정안전부")
    parser.add_argument("--destination", default="무안국제공항")
    parser.add_argument("--case", default="sample", choices=("sample", "ambiguous"))
    parser.add_argument("--inspect-fuel", action="store_true")
    args = parser.parse_args()
    output = ROOT / "artifacts" / "verification" / "naver"
    output.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    exceptions: list[dict[str, str | int]] = []

    def trace(frame, event, arg):
        if event == "exception" and frame.f_code.co_filename.endswith("sites.py"):
            kind, error, _ = arg
            if issubclass(kind, Exception):
                exceptions.append({"function": frame.f_code.co_name, "line": frame.f_lineno,
                                   "type": kind.__name__, "message": str(error)})
        return trace

    def log(message: str) -> None:
        messages.append(message)
        print(message, flush=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
        context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.dismiss()))
        sys.settrace(trace)
        prepared = prepare_naver(context, RunRequest("2026-09-30", args.origin, args.destination), log)
        sys.settrace(None)
        page = prepared.page
        page.screenshot(path=str(output / f"{args.case}.png"), full_page=True)
        (output / f"{args.case}.html").write_text(page.content(), encoding="utf-8")
        (output / f"{args.case}-aria.txt").write_text(page.locator("body").aria_snapshot(), encoding="utf-8")
        inputs = {name: page.get_by_role("combobox", name=name, exact=True).input_value()
                  for name in ("출발지 입력", "도착지 입력")}
        report = {"ready": prepared.ready, "message": prepared.message, "details": prepared.details,
                  "url": page.url, "title": page.title(), "log": messages, "exceptions": exceptions,
                  "inputs": inputs}
        (output / f"{args.case}-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.inspect_fuel and prepared.ready:
            page.get_by_role("button", name=re.compile(r"1종.*차량 기준")).click()
            (output / "fuel-options-aria.txt").write_text(page.locator("body").aria_snapshot(), encoding="utf-8")
            (output / "fuel-options.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(output / "fuel-options.png"), full_page=True)
            print(page.locator("body").aria_snapshot(), flush=True)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
