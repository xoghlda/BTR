from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from trip_capture.automation import AutomationWorker, belongs_to_site, make_output_directory
from trip_capture.models import PreparedPage, RunRequest
from trip_capture.platformpaths import load_settings, save_settings


class CoreTests(unittest.TestCase):
    def test_settings_never_write_password_or_unknown_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch("trip_capture.platformpaths.app_data_dir", return_value=folder):
                save_settings({"hipass_id": "example", "browser": "chrome", "password": "SHOULD_NOT_EXIST", "token": "SHOULD_NOT_EXIST"})
                raw = (folder / "settings.json").read_text(encoding="utf-8")
                self.assertNotIn("SHOULD_NOT_EXIST", raw)
                self.assertEqual(load_settings(), {"hipass_id": "example", "browser": "chrome"})
                (folder / "settings.json").write_text("{broken", encoding="utf-8")
                self.assertEqual(load_settings(), {})

    def test_capture_rejects_login_and_other_site_before_writing(self):
        worker = AutomationWorker(queue.Queue())
        page = Mock()
        page.is_closed.return_value = False
        for url in ("https://example.com/receipt", "https://www.hipass.co.kr/comm/lginpg.do", "https://www.hipass.co.kr/comm/anyidLogin.do?tx=secret"):
            page.url = url
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    worker._capture("hipass", PreparedPage(page), Path(temporary), manual=False)
                page.screenshot.assert_not_called()
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_capture_domain_checks_do_not_accept_lookalikes(self):
        self.assertTrue(belongs_to_site("https://www.opinet.co.kr/user/dopospdrg/", "opinet"))
        self.assertFalse(belongs_to_site("https://opinet.co.kr.evil.test/user/", "opinet"))
        self.assertFalse(belongs_to_site("https://notopinet.co.kr/", "opinet"))

    def test_repeated_runs_keep_separate_directories_and_safe_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = RunRequest("2026-09-30", "세종", "../../무안:공항?", output_dir=root)
            first, second = make_output_directory(request), make_output_directory(request)
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, root.resolve())
            self.assertNotIn(":", first.name)
            self.assertNotIn("?", first.name)

    def test_car_and_train_outputs_are_named_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            car = RunRequest("2026-09-30", "세종", "서울", output_dir=root, transport_mode="car")
            train = RunRequest("2026-09-30", "세종", "서울", sites=("korail",), output_dir=root, transport_mode="train")
            self.assertIn("_자차_", make_output_directory(car).name)
            self.assertIn("_기차_", make_output_directory(train).name)

    def test_user_skip_does_not_produce_a_capture(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        worker.skip_current()
        page = Mock()
        result = worker._manual_capture("hipass", PreparedPage(page, message="로그인 필요"), Path("unused"))
        self.assertIsNone(result)
        page.screenshot.assert_not_called()

    def test_cancelled_wait_returns_without_capture(self):
        worker = AutomationWorker(queue.Queue())
        worker._context = Mock(pages=[])
        worker.cancel()
        page = Mock()
        self.assertIsNone(worker._manual_capture("hipass", PreparedPage(page), Path("unused")))
        page.screenshot.assert_not_called()

    def test_manual_capture_uses_explicitly_selected_page(self):
        worker = AutomationWorker(queue.Queue())
        wanted, old_popup = Mock(), Mock()
        wanted.url = old_popup.url = "https://www.hipass.co.kr/receipts.do"
        wanted.is_closed.return_value = old_popup.is_closed.return_value = False
        worker._context = Mock(pages=[wanted, old_popup])
        self.assertIs(worker._choose_page("hipass", PreparedPage(old_popup), str(id(wanted))), wanted)
        with self.assertRaises(RuntimeError):
            worker._choose_page("hipass", PreparedPage(old_popup), "closed-page-id")

    def test_external_receipt_capture_is_copied_without_browser_login(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            staging = base / "staging"
            staging.mkdir()
            image = staging / "native-test.png"
            Image.new("RGB", (300, 200), "white").save(image)
            original = image.read_bytes()
            output = base / "output"
            output.mkdir()
            worker = AutomationWorker(queue.Queue())
            worker._context = Mock(pages=[])
            worker.capture_external(image, "테스트 영수증 창")
            with patch("trip_capture.automation.app_data_dir", return_value=base):
                record = worker._manual_capture("hipass", PreparedPage(Mock()), output)
            self.assertTrue(record.manual)
            self.assertEqual(record.image_path.read_bytes(), original)
            self.assertFalse(image.exists())
            self.assertIn("전체 창", record.details["저장 방식"])

    def test_capture_refuses_password_form_on_unexpected_url(self):
        worker = AutomationWorker(queue.Queue())
        page = Mock()
        page.url = "https://www.hipass.co.kr/otherpage.do"
        page.is_closed.return_value = False
        password_field = Mock()
        password_field.is_visible.return_value = True
        frame = Mock()
        frame.locator.return_value.all.return_value = [password_field]
        page.frames = [frame]
        with self.assertRaisesRegex(RuntimeError, "비밀번호"):
            worker._capture("hipass", PreparedPage(page), Path("unused"), manual=False)
        page.screenshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
