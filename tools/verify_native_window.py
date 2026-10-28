"""Exercise native screen capture on a new, unsigned-in public Opinet window."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from trip_capture.native_capture import (
    activate_browser_window,
    list_browser_windows,
    save_visible_browser_window,
)


def main() -> int:
    output = Path(__file__).resolve().parents[1] / "artifacts" / "verification" / "native"
    output.mkdir(parents=True, exist_ok=True)
    before = {window.hwnd for window in list_browser_windows()}
    result = {"public_sample": True, "uses_existing_profile": False, "success": False}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome", headless=False,
                args=["--window-size=1100,900", "--window-position=50,50"],
            )
            try:
                context = browser.new_context(viewport={"width": 1080, "height": 780}, locale="ko-KR")
                page = context.new_page()
                page.goto("https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do", wait_until="domcontentloaded", timeout=30000)
                page.locator("body").wait_for(state="visible", timeout=10000)
                page.wait_for_timeout(800)
                created = [window for window in list_browser_windows() if window.hwnd not in before and window.browser == "Chrome"]
                if len(created) != 1:
                    raise RuntimeError(f"New isolated browser window was not unique: {len(created)} candidates")
                window = created[0]
                activate_browser_window(window)
                page.wait_for_timeout(700)
                screenshot = output / "screen.png"
                save_visible_browser_window(window, screenshot)
                result.update(success=True, image=str(screenshot), browser=window.browser, title=window.title, url=page.url)
            finally:
                browser.close()
    except Exception as exc:
        result["error"] = str(exc)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
