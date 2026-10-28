"""Exercise automatic capture, manual login refusal, skip and cleanup."""
import json
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from trip_capture.automation import AutomationWorker
from trip_capture.models import RunRequest


class CheckWorker(AutomationWorker):
    def _ensure_browser(self, requested):
        self._playwright = sync_playwright().start()
        self._test_browser = self._playwright.chromium.launch(channel=requested, headless=True)
        self._context = self._test_browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", timezone_id="Asia/Seoul")
        self._context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.dismiss()))
        return self._context


def main():
    events = queue.Queue()
    worker = CheckWorker(events)
    worker.start()
    output = Path(__file__).resolve().parents[1] / "artifacts" / "verification" / "worker"
    worker.submit(RunRequest("2026-09-30", "행정안전부", "무안국제공항", sites=("opinet", "hipass"), output_dir=output))
    deadline = time.monotonic() + 100
    manual_attempted = False
    rejected_login = False
    complete = None
    while time.monotonic() < deadline:
        event = events.get(timeout=40)
        print(json.dumps(event, ensure_ascii=True))
        if event["type"] == "manual" and manual_attempted:
            rejected_login = True
        if event["type"] == "pages":
            if not manual_attempted:
                manual_attempted = True
                worker.capture_current(event["selected_id"])
            else:
                worker.skip_current()
        if event["type"] == "done":
            complete = event
            break
    worker.shutdown()
    worker.join(timeout=30)
    assert complete and complete["count"] == 1, "automatic screenshot was not saved"
    assert rejected_login, "login screen capture was not refused"
    assert not worker.is_alive(), "browser worker failed to shut down"
    print("WORKER_LIVE_CHECK_OK")


if __name__ == "__main__":
    main()
