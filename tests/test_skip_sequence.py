"""Exercise site skipping through the real request/manual-command sequence.

Site pages, screenshot creation and report writing are mocks.  No browser,
account, personal PDF, or real screenshot is opened by these tests.
"""

from __future__ import annotations

import queue
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from trip_capture.automation import AutomationWorker
from trip_capture.models import CaptureRecord, PreparedPage, RunRequest, SITE_LABELS


ADAPTERS = {
    "naver": "trip_capture.sites.prepare_naver",
    "opinet": "trip_capture.sites.prepare_opinet",
    "hipass": "trip_capture.sites.prepare_hipass",
    "korail": "trip_capture.korail.prepare_korail",
}
URLS = {
    "naver": "https://map.naver.com/p/directions",
    "opinet": "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do",
    "hipass": "https://www.hipass.co.kr/usepculr/InitUsePculrTabSearch.do",
    "korail": "https://www.korail.com/ticket/mypage/ticketInfo/receipt",
}


class SkipSequenceTests(unittest.TestCase):
    def run_sequence(self, sites, *, automatic=(), commands=()):
        events = queue.Queue()
        worker = AutomationWorker(events)
        pages = {}
        for site, url in URLS.items():
            page = Mock(name=site + "_fixture_page")
            page.url = url
            page.is_closed.return_value = False
            page.opener.return_value = None
            page.title.return_value = SITE_LABELS[site] + " 개발 검증 화면"
            pages[site] = page
        context = Mock(pages=list(pages.values()))
        worker._context = context
        prepared_sites, captured_sites, report_snapshots = [], [], []
        records = {
            site: CaptureRecord(site, SITE_LABELS[site], Path(f"fixture-{site}.png"), URLS[site],
                                "2026-10-23T12:00:00+09:00", manual=site not in automatic)
            for site in sites
        }

        def adapter_for(site):
            def prepare(_context, _request, _log, **_kwargs):
                self.assertIs(_context, context)
                prepared_sites.append(site)
                return PreparedPage(pages[site], ready=site in automatic,
                                    message="개발용 수동 단계: " + site)
            return prepare

        def capture(site, prepared, directory, manual, page_id=None):
            self.assertIs(prepared.page, pages[site])
            self.assertEqual(manual, site not in automatic)
            if site == "korail":
                self.assertEqual(page_id, "korail-receipt-fixture")
            captured_sites.append(site)
            return records[site]

        def report(_request, captured, directory, notices):
            # Snapshot the mutable collection at every report save.
            report_snapshots.append(list(captured))
            return directory / "travel_report.html"

        for command in commands:
            if command == "skip":
                worker.skip_current()
            elif command == "capture_korail":
                worker.capture_current("korail-receipt-fixture")
            elif command == "finish":
                worker.finish_receipts()
            else:
                self.fail("Unknown fixture command: " + command)

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            request = RunRequest("2026-10-20", "가상 출발지", "가상 도착지", sites=tuple(sites),
                                 output_dir=Path(temporary), rail_origin="오송", rail_destination="서울")
            stack.enter_context(patch.object(worker, "_ensure_browser", return_value=context))
            stack.enter_context(patch.object(worker, "_capture", side_effect=capture))
            stack.enter_context(patch("trip_capture.automation.write_report", side_effect=report))
            for site, adapter in ADAPTERS.items():
                stack.enter_context(patch(adapter, side_effect=adapter_for(site)))
            worker._run_request(request)

        return worker, list(events.queue), prepared_sites, captured_sites, records, report_snapshots

    def assert_steps(self, events, sites):
        steps = [event for event in events if event["type"] == "step"]
        self.assertEqual([event["site"] for event in steps], list(sites))
        self.assertEqual([event["index"] for event in steps], list(range(1, len(sites) + 1)))
        self.assertTrue(all(event["total"] == len(sites) for event in steps))
        self.assertTrue(all(tuple(event["sites"]) == tuple(sites) for event in steps))
        self.assertEqual([event["next_site"] for event in steps[:-1]], list(sites[1:]))
        self.assertIn(steps[-1]["next_site"], (None, ""))

    def assert_finished_without_shutdown(self, worker, events):
        self.assertFalse(worker.stopping.is_set(), "Skipping a site must not shut down the worker")
        self.assertFalse(worker.cancelled.is_set(), "Skipping a site must not cancel the request")
        states = [event["value"] for event in events if event["type"] == "state"]
        self.assertEqual(states[-1], "idle")
        self.assertEqual(len([event for event in events if event["type"] == "done"]), 1)

    def test_skipping_first_three_sites_still_enters_korail_and_preserves_its_capture(self):
        sites = ("naver", "opinet", "hipass", "korail")
        worker, events, prepared, captured, records, reports = self.run_sequence(
            sites, commands=("skip", "skip", "skip", "capture_korail", "finish"))

        self.assertEqual(prepared, list(sites))
        self.assertEqual(captured, ["korail"])
        self.assert_steps(events, sites)
        done = next(event for event in events if event["type"] == "done")
        self.assertEqual(done["count"], 1)
        self.assertEqual(done["site_count"], 1)
        self.assertEqual(done["skipped_sites"], list(sites[:3]))
        self.assertEqual(reports[-1], [records["korail"]])
        self.assertTrue(any(snapshot == [records["korail"]] for snapshot in reports[:-1]),
                        "The Korail receipt should also be present in a partial report")
        self.assert_finished_without_shutdown(worker, events)

    def test_skipping_final_hipass_keeps_both_automatic_captures_and_emits_done_once(self):
        sites = ("naver", "opinet", "hipass")
        worker, events, prepared, captured, records, reports = self.run_sequence(
            sites, automatic=("naver", "opinet"), commands=("skip",))

        self.assertEqual(prepared, list(sites))
        self.assertEqual(captured, ["naver", "opinet"])
        self.assert_steps(events, sites)
        done = next(event for event in events if event["type"] == "done")
        self.assertEqual(done["count"], 2)
        self.assertEqual(done["site_count"], 2)
        self.assertEqual(done["skipped_sites"], ["hipass"])
        self.assertEqual(reports[-1], [records["naver"], records["opinet"]])
        self.assert_finished_without_shutdown(worker, events)


if __name__ == "__main__":
    unittest.main()
