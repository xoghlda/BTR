"""Offline, synthetic full-workflow smoke check. Never uses native input."""
from dataclasses import replace
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import shutil
import sys
import threading
import time

from .browser_macro import Geometry, WindowIdentity, WindowSnapshot
from .models import RunRequest
from .native_capture import NativeBrowserWindow
from .workflow import WorkflowEngine, WorkflowProfile, WorkflowStep, load_workflow, save_workflow
from . import workflow_evidence


class _SyntheticAdapter:
    def __init__(self, source: Path, pdf: Path):
        self.window = NativeBrowserWindow(101, "개발용 로그인", "Edge")
        self.popup = NativeBrowserWindow(102, "개발용 영수증", "Edge")
        self.identity = WindowIdentity(101, 123, 456, r"C:\BTR-FAKE\msedge.exe")
        self.geometry = Geometry(800, 600, 96)
        self.source, self.pdf = source, pdf
        self.url = "https://www.korail.com/ticket/login"
        self.clock = 0.0
        self.clicked = []
        self.typed = []
        self.popup_opened = False
        self.prepared = []

    def list_windows(self):
        return [self.window] + ([self.popup] if self.popup_opened else [])

    def bind(self, window):
        return replace(self.identity, hwnd=window.hwnd)

    def prepare_window(self, identity, mode, expected_geometry=None, *, stopped=None):
        self.prepared.append(identity.hwnd)
        return self.snapshot(identity)

    def snapshot(self, identity):
        return WindowSnapshot(identity, self.geometry, 50, 80)

    def cursor_position(self):
        return 400, 300

    def escape_pressed(self):
        return False

    def left_click(self, identity, geometry, x, y):
        self.clicked.append((identity.hwnd, x, y))
        if x == 90:
            self.popup_opened = True

    def type_text(self, identity, geometry, text, *, clear=True, stopped=None):
        self.typed.append(text)

    def press_key(self, identity, geometry, key, *, stopped=None):
        if key == "CTRL+P":
            shutil.copyfile(self.source, self.pdf)

    def navigate(self, identity, geometry, url, *, stopped=None):
        self.url = url

    def current_url(self, identity):
        return self.url

    def monotonic(self):
        return self.clock

    def wait(self, seconds, stopped):
        self.clock += seconds
        time.sleep(0.001)  # Let the synthetic controller receive pause events.


def run_workflow_selfcheck(source: Path, output_dir: Path) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    pdf = output / 'new-synthetic-receipt.pdf'
    if pdf.exists():
        # This exact fixture output belongs to this self-check, never a user path.
        pdf.unlink()
    result = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)),
              'fixture_only': True, 'real_input_sent': False, 'native_windows_calls': 0,
              'network_used': False, 'credentials_used': False, 'checks': {}}
    checks = result['checks']
    checks['native_dependency_available'] = importlib.util.find_spec('comtypes') is not None
    checks['bundled_dependency_license'] = not result['frozen'] or (Path(sys._MEIPASS) / 'licenses/comtypes-LICENSE.txt').is_file()
    adapter = _SyntheticAdapter(source, pdf)
    events = queue.Queue()
    request = RunRequest(trip_date=date.today().isoformat(), origin='가상 출발', destination='가상 도착',
                         sites=('korail',), transport_mode='train', output_dir=output / 'reports')
    collector = workflow_evidence.WorkflowEvidence(request)
    capture_original = workflow_evidence.save_visible_browser_window
    capture_windows = []

    def fake_capture(window, path):
        from PIL import Image
        capture_windows.append(window.hwnd)
        with Image.new('RGB', (80, 60), 'white') as image:
            image.save(path)

    workflow_evidence.save_visible_browser_window = fake_capture
    engine = WorkflowEngine(events, adapter=adapter, collector=collector)
    try:
        steps = (
            WorkflowStep('open_url', '개발용 로그인 이동', 'korail', value='https://www.korail.com/ticket/login', wait_ms=200),
            WorkflowStep('username', '아이디 입력 연습', 'korail', 10, 10, wait_ms=200),
            WorkflowStep('password', '비밀번호 입력 연습', 'korail', 20, 20, wait_ms=200),
            WorkflowStep('pause', '개발용 인증 대기', 'korail', wait_ms=200),
            WorkflowStep('text', '출장일 입력 연습', 'korail', 30, 30, '{trip_date_compact}', 200),
            WorkflowStep('click', '개발용 영수증 창 열기', 'korail', 90, 90, wait_ms=200),
            WorkflowStep('switch_window', '개발용 새 창 전환', 'korail', value='개발용 영수증', wait_ms=200),
            WorkflowStep('capture', '개발용 화면 저장', 'korail', wait_ms=200),
            WorkflowStep('key', '개발용 PDF 생성', 'korail', value='CTRL+P', wait_ms=200),
            WorkflowStep('pdf', '개발용 새 PDF 수집', 'korail', value='receipt_pdf', wait_ms=200),
        )
        profile = WorkflowProfile('Edge', adapter.geometry, steps)
        path = output / 'synthetic.btr-workflow.json'
        save_workflow(path, profile)
        profile = load_workflow(path)
        serialized = path.read_text(encoding='utf-8')
        checks['credential_values_absent_from_json'] = 'DEVELOPMENT_USER_ONLY' not in serialized and 'DEVELOPMENT_PASSWORD_ONLY' not in serialized
        job = engine.play(adapter.window, profile,
            accounts={'korail': ('DEVELOPMENT_USER_ONLY', 'DEVELOPMENT_PASSWORD_ONLY')},
            variables={'trip_date_compact': date.today().strftime('%Y%m%d')},
            pdf_paths={'receipt_pdf': pdf})
        finished = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            event = events.get(timeout=5)
            if event['job_id'] != job:
                continue
            if event['type'] == 'paused':
                checks['authentication_pause_resumed'] = True
                engine.resume(job)
            if event['type'] in ('done', 'error', 'stopped'):
                finished = event
            if event['type'] == 'idle':
                break
        checks['all_workflow_steps_completed'] = bool(finished and finished['type'] == 'done' and finished['count'] == len(steps))
        checks['memory_only_credentials_typed'] = adapter.typed[:2] == ['DEVELOPMENT_USER_ONLY', 'DEVELOPMENT_PASSWORD_ONLY']
        checks['trip_variable_expanded'] = date.today().strftime('%Y%m%d') in adapter.typed
        checks['new_window_selected_for_capture'] = capture_windows == [102]
        checks['screenshot_and_three_pdf_pages'] = len(collector.records) == 4
        checks['original_pdf_unchanged'] = collector.records[-1].original_path.read_bytes() == source.read_bytes()
        report = collector.report
        checks['train_form_generated'] = bool(report and (report.parent / 'rail_settlement.html').is_file() and not (report.parent / 'car_settlement.html').exists())
        content = (report.parent / 'manifest.json').read_text(encoding='utf-8') if report else ''
        checks['credential_values_absent_from_report'] = all(marker not in content for marker in ('DEVELOPMENT_USER_ONLY', 'DEVELOPMENT_PASSWORD_ONLY'))
        checks['worker_idle'] = not engine.is_busy()
        result['ok'] = all(checks.values())
        result['report'] = str(report) if report else ''
        result['pdf_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        if not result['ok']:
            result['error'] = 'Synthetic full-workflow check failed; inspect check flags.'
    except Exception as error:
        result['error'] = type(error).__name__
    finally:
        engine.close()
        workflow_evidence.save_visible_browser_window = capture_original
        adapter.typed.clear()
        checks['worker_closed'] = not engine.is_busy()
        result['ok'] = bool(result['ok'] and checks['worker_closed'])
        (output / 'runtime_check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result
