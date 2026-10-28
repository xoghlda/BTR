from __future__ import annotations

import sys
import traceback
from pathlib import Path


def self_check(output_dir: Path) -> None:
    """Developer packaging check, isolated from credentials and saved profiles."""
    import json
    from tkinter import Tk
    from playwright.sync_api import sync_playwright
    from trip_capture.models import RunRequest, CaptureRecord
    from trip_capture.report import write_report
    from datetime import datetime, timedelta, timezone

    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        root = Tk()
        root.withdraw()
        summary["tk"] = root.tk.call("info", "patchlevel")
        root.destroy()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", timezone_id="Asia/Seoul")
            context.on("page", lambda page: page.on("dialog", lambda dialog: dialog.dismiss()))
            from trip_capture.sites import prepare_opinet
            request = RunRequest("2026-09-30", "행정안전부", "무안국제공항", sites=("opinet",), output_dir=output_dir)
            prepared = prepare_opinet(context, request, lambda message: None)
            summary["opinet_ready"] = prepared.ready
            if not prepared.ready:
                raise RuntimeError(prepared.message)
            screenshot = output_dir / "opinet_packaged_check.png"
            prepared.page.screenshot(path=str(screenshot), full_page=True)
            record = CaptureRecord("opinet", "오피넷 일일 유가 · 실행 파일 검증", screenshot, prepared.page.url,
                                   datetime.now(timezone(timedelta(hours=9))).isoformat(), prepared.details)
            write_report(request, [record], output_dir)
            browser.close()
        summary["ok"] = True
    except Exception as exc:
        summary["error"] = type(exc).__name__ + ": " + str(exc)
    (output_dir / "runtime_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def self_check_manual_korail(source: Path, output_dir: Path) -> None:
    """Exercise the real browserless worker with a fictional developer PDF."""
    import hashlib
    import json
    import queue
    import pypdfium2 as pdfium
    from trip_capture.automation import AutomationWorker
    from trip_capture.models import RunRequest

    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "ok": False, "frozen": bool(getattr(sys, "frozen", False)), "fixture_only": True,
        "browser_launch_attempts": 0, "waiting_before_browser": False,
        "emptyfinish_keptwaiting": False, "manual_counts": [], "managed_page_counts": [],
    }
    try:
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        document = pdfium.PdfDocument(str(source))
        try:
            expected_pages = len(document)
        finally:
            document.close()

        class GuardedWorker(AutomationWorker):
            def _ensure_browser(self, requested):
                result["browser_launch_attempts"] += 1
                raise RuntimeError("Manual Korail self-check forbids launching a managed browser")

        class DriverEvents(queue.Queue):
            phase = 0

            def put(self, item, block=True, timeout=None):
                super().put(item, block=block, timeout=timeout)
                if item.get("type") == "pages":
                    result["managed_page_counts"].append(len(item.get("pages", [])))
                if item.get("type") != "manual":
                    return
                count = item.get("collected_count", 0)
                result["manual_counts"].append(count)
                if not item.get("browserless"):
                    result["driver_error"] = "The worker did not enter ordinary-browser collection"
                    worker.cancel()
                elif self.phase == 0:
                    result["waiting_before_browser"] = result["browser_launch_attempts"] == 0
                    self.phase = 1
                    worker.finish_receipts()
                elif self.phase == 1:
                    result["emptyfinish_keptwaiting"] = count == 0
                    self.phase = 2
                    worker.import_pdf(source)
                elif self.phase == 2 and count > 0:
                    self.phase = 3
                    worker.finish_receipts()
                elif self.phase == 2:
                    result["driver_error"] = "The selected development PDF was not imported"
                    worker.cancel()

        events = DriverEvents()
        worker = GuardedWorker(events)
        request = RunRequest(
            "2026-10-20", "개발용 출발지", "개발용 출장지", sites=("korail",), transport_mode="train",
            output_dir=output_dir, trip_end_date="2026-10-21", department="개발용 검증자료",
            traveler_name="가상 사용자", trip_purpose="프로그램 검증용 · 실제 지출 증빙 아님",
            rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송",
        )
        worker._run_request(request)
        done_events = [event for event in events.queue if event["type"] == "done"]
        if len(done_events) != 1:
            raise RuntimeError("Expected one completed manual collection")
        done = done_events[0]
        directory = Path(done["output_dir"])
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        originals = metadata.get("attachments", [])
        original_preserved = len(originals) == 1 and originals[0]["sha256"] == source_hash
        if original_preserved:
            original_preserved = hashlib.sha256((directory / originals[0]["path"]).read_bytes()).hexdigest() == source_hash
        input_unchanged = hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        pages = len(metadata.get("captures", []))
        settlement = directory / "rail_settlement.html"
        checks = (
            result["waiting_before_browser"], result["emptyfinish_keptwaiting"],
            result["browser_launch_attempts"] == 0, not result.get("driver_error"),
            all(count == 0 for count in result["managed_page_counts"]),
            pages == expected_pages > 0, metadata.get("status") == "complete",
            original_preserved, input_unchanged, settlement.is_file(),
        )
        result.update(
            ok=all(checks), pages=pages, expected_pages=expected_pages,
            original_sha256=source_hash, original_unchanged=original_preserved and input_unchanged,
            report=done["report_path"], settlement=str(settlement),
            report_status=metadata.get("status"), transport_mode=metadata["trip"]["transport_mode"],
            rail_origin=metadata["trip"]["rail_origin"], rail_destination=metadata["trip"]["rail_destination"],
        )
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    (output_dir / "runtime_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--self-check-manual-korail":
        self_check_manual_korail(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--self-check-modes":
        import hashlib
        import json
        from PIL import Image, ImageDraw
        from trip_capture.models import CaptureRecord, RunRequest
        from trip_capture.pdf_receipts import import_receipt_pdf
        from trip_capture.report import write_report
        source = Path(sys.argv[2])
        target = Path(sys.argv[3])
        target.mkdir(parents=True, exist_ok=True)
        result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "fixture_only": True}
        try:
            car_dir, train_dir = target / "car", target / "train"
            car_dir.mkdir()
            train_dir.mkdir()
            originals = {}
            records = []
            for site in ("naver", "opinet", "hipass"):
                image_path = car_dir / f"development_{site}.png"
                with Image.new("RGB", (650, 450), "white") as fixture:
                    draw = ImageDraw.Draw(fixture)
                    draw.text((24, 30), "DEVELOPMENT FIXTURE ONLY - NOT A VALID RECEIPT", fill="red")
                    draw.text((24, 80), "CAR MODE TEST: " + site, fill="black")
                    fixture.save(image_path)
                originals[image_path] = hashlib.sha256(image_path.read_bytes()).hexdigest()
                records.append(CaptureRecord(site, "개발용 가상자료 · " + site, image_path, "", "2026-10-24T12:00:00+09:00", manual=True))
            common = {"trip_end_date": "2026-10-21", "department": "개발용 검증자료", "traveler_name": "가상 사용자", "trip_purpose": "프로그램 검증용 · 실제 지출 증빙 아님"}
            car = RunRequest("2026-10-20", "개발용 출발지", "개발용 출장지", transport_mode="car", **common)
            car.validate()
            car_report = write_report(car, records, car_dir)
            train_records = import_receipt_pdf(source, train_dir, site="korail")
            train = RunRequest("2026-10-20", "개발용 출발지", "개발용 출장지", sites=("korail",), transport_mode="train",
                               rail_origin="오송", rail_destination="서울", rail_return_origin="용산", rail_return_destination="오송", **common)
            train.validate()
            train_report = write_report(train, train_records, train_dir)
            checks = {
                "car_settlement": (car_dir / "car_settlement.html").is_file(),
                "train_settlement": (train_dir / "rail_settlement.html").is_file(),
                "no_mixed_forms": not (car_dir / "rail_settlement.html").exists() and not (train_dir / "car_settlement.html").exists(),
                "mode_titles": "자차 이용 출장보고서" in car_report.read_text(encoding="utf-8") and "기차 이용 출장보고서" in train_report.read_text(encoding="utf-8"),
                "original_images_unchanged": all(hashlib.sha256(path.read_bytes()).hexdigest() == checksum for path, checksum in originals.items()),
                "original_pdf_unchanged": source.read_bytes() == train_records[0].original_path.read_bytes(),
            }
            result.update(ok=all(checks.values()), checks=checks, car_report=str(car_report), train_report=str(train_report), pdf_pages=len(train_records))
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        (target / "runtime_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--self-check-korail":
        import hashlib
        import json
        from trip_capture.pdf_receipts import import_receipt_pdf
        from trip_capture.models import RunRequest
        from trip_capture.report import write_report
        source = Path(sys.argv[2])
        target = Path(sys.argv[3])
        target.mkdir(parents=True, exist_ok=True)
        result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "fixture_only": True}
        try:
            records = import_receipt_pdf(source, target, site="korail")
            request = RunRequest("2026-10-20", "개발용 출발지", "개발용 출장지", sites=("korail",),
                                 trip_end_date="2026-10-21", department="개발용 검증자료", traveler_name="가상 사용자",
                                 trip_purpose="프로그램 검증용 · 실제 지출 증빙 아님", rail_origin="오송", rail_destination="서울",
                                 rail_return_origin="용산", rail_return_destination="오송", transport_mode="train")
            report = write_report(request, records, target)
            unchanged = hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(records[0].original_path.read_bytes()).digest()
            settlement = target / "rail_settlement.html"
            result.update(ok=unchanged and settlement.is_file(), pages=len(records), original_unchanged=unchanged,
                          report=str(report), settlement=str(settlement))
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        (target / "runtime_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--self-check-pdf":
        import json
        from trip_capture.pdf_receipts import import_receipt_pdf
        from trip_capture.models import RunRequest
        from trip_capture.report import write_report
        target = Path(sys.argv[3])
        target.mkdir(parents=True, exist_ok=True)
        result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
        try:
            records = import_receipt_pdf(Path(sys.argv[2]), target)
            report = write_report(RunRequest("2026-09-30", "PDF 기능 검증", "공개 테스트 자료", sites=("hipass",)), records, target)
            result.update(ok=True, pages=len(records), report=str(report))
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        (target / "runtime_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--self-check":
        self_check(Path(sys.argv[2]))
        return
    # One process per user avoids sharing a locked Chrome profile between app instances.
    mutex = None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        mutex = kernel.CreateMutexW(None, False, "Local\\TripEvidenceCapture.Desktop.v1")
        if mutex and ctypes.get_last_error() == 183:
            from tkinter import Tk, messagebox
            root = Tk()
            root.withdraw()
            messagebox.showinfo("출장 증빙 캡처", "프로그램이 이미 실행 중입니다. 기존 '출장 증빙 캡처' 창을 닫고 새 실행 파일을 다시 열어 주세요.")
            root.destroy()
            kernel.CloseHandle(mutex)
            return
        try:
            ctypes.WinDLL("user32").SetProcessDPIAware()
        except Exception:
            pass
    try:
        from trip_capture.gui import main as gui_main
        gui_main()
    except Exception:
        from tkinter import Tk, messagebox
        from trip_capture.platformpaths import app_data_dir
        log = app_data_dir() / "startup-error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        root = Tk()
        root.withdraw()
        messagebox.showerror("출장 증빙 캡처", f"프로그램을 시작하지 못했습니다.\n오류 기록: {log}")
        root.destroy()
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
