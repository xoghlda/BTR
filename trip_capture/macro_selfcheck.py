"""Packaged/source macro smoke check using only a fake adapter and clock."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import queue
import sys
import threading

from .browser_macro import (
    BrowserMacro, ClickStep, Geometry, MacroProfile, WindowIdentity,
    WindowSnapshot, load_profile, save_profile,
)
from .native_capture import NativeBrowserWindow


class _FixtureAdapter:
    """In-memory browser state; this class has no operating-system input path."""

    def __init__(self):
        self.window = NativeBrowserWindow(101, "개발용 가상 브라우저", "Edge")
        self.identity = WindowIdentity(101, 202, 303, "msedge.exe")
        self.geometry = Geometry(800, 600, 96)
        self.state = WindowSnapshot(self.identity, self.geometry, 100, 100)
        self.pointer = (150, 180)
        self.clock = 0.0
        self.sent: list[tuple[int, int]] = []
        self.prepared_modes: list[str] = []
        self.resize_during_countdown = False

    def list_windows(self):
        return [self.window]

    def bind(self, window):
        if window != self.window:
            raise ValueError("Fixture window mismatch")
        return self.identity

    def snapshot(self, identity):
        return self.state

    def prepare_window(self, identity, mode, expected_geometry=None, *, stopped=None):
        self.prepared_modes.append(mode)
        target = self.geometry if mode == "maximize" else (expected_geometry or self.state.geometry)
        self.state = replace(self.state, geometry=target, minimized=False, visible=True)
        return self.state

    def cursor_position(self):
        return self.pointer

    def escape_pressed(self):
        return False

    def left_click(self, identity, geometry, x, y):
        self.pointer = (self.state.left + x, self.state.top + y)
        self.sent.append(self.pointer)
        if len(self.sent) == 1:
            self.state = replace(self.state, left=350, top=220)

    def monotonic(self):
        return self.clock

    def wait(self, seconds: float, stopped: threading.Event):
        self.clock += seconds
        if self.resize_during_countdown:
            self.state = replace(self.state, geometry=Geometry(900, 600, 96))


def _job_events(events: queue.Queue, job_id: str) -> list[dict]:
    result = []
    while True:
        event = events.get(timeout=5)
        if event.get("job_id") != job_id:
            raise ValueError("Unexpected fixture job event")
        result.append(event)
        if event["type"] == "idle":
            return result


def _require(value: bool, message: str):
    if not value:
        raise ValueError(message)


def run_macro_selfcheck(output_dir: Path) -> dict:
    """Exercise the real engine with fake coordinates; write runtime_check.json."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    adapter = _FixtureAdapter()
    events: queue.Queue = queue.Queue()
    engine = BrowserMacro(events, adapter=adapter)
    checks: dict[str, bool] = {}
    result = {
        "ok": False, "frozen": bool(getattr(sys, "frozen", False)),
        "implementation": "real BrowserMacro engine with in-memory fake adapter and clock",
        "real_input_sent": False, "native_windows_calls": 0,
        "network_used": False, "credentials_used": False, "checks": checks,
    }
    try:
        recorded_events = _job_events(events, engine.record_point(adapter.window, "개발용 클릭 1", 250))
        record = next(event for event in recorded_events if event["type"] == "recorded")
        checks["recorded_relative_point"] = record["step"] == ClickStep("개발용 클릭 1", 50, 80, 250)
        checks["recording_sent_no_clicks"] = not adapter.sent
        checks["four_second_countdown"] = [event["remaining"] for event in recorded_events if event["type"] == "countdown"] == [4, 3, 2, 1] and adapter.clock >= 4
        profile = MacroProfile(record["browser"], record["geometry"], (
            record["step"], ClickStep("개발용 클릭 2", 90, 120, 250), ClickStep("개발용 클릭 3", 130, 160, 250),
        ), record["window_mode"])
        checks["default_maximize_prepared"] = record["window_mode"] == "maximize" and adapter.prepared_modes == ["maximize"]
        profile_path = output / "macro_profile.json"
        save_profile(profile_path, profile)
        loaded = load_profile(profile_path)
        checks["profile_roundtrip"] = loaded == profile
        checks["profile_safe_schema"] = set(json.loads(profile_path.read_text(encoding="utf-8"))) == {"version", "browser", "geometry", "steps", "window_mode"}
        playback_events = _job_events(events, engine.play(adapter.window, loaded))
        checks["three_clicks_completed"] = any(event["type"] == "done" and event["count"] == 3 for event in playback_events)
        checks["moved_window_coordinates"] = adapter.sent == [(150, 180), (440, 340), (480, 380)]
        count = len(adapter.sent)
        adapter.state = replace(adapter.state, foreground=False)
        focus_events = _job_events(events, engine.play(adapter.window, loaded))
        checks["focus_guard_no_extra_clicks"] = any(event["type"] == "error" for event in focus_events) and len(adapter.sent) == count
        adapter.state = replace(adapter.state, foreground=True)
        adapter.resize_during_countdown = True
        resize_events = _job_events(events, engine.play(adapter.window, loaded))
        checks["resize_guard_no_extra_clicks"] = any(event["type"] == "error" for event in resize_events) and len(adapter.sent) == count
        adapter.resize_during_countdown = False
        old = profile.to_dict()
        old["version"] = 1
        old.pop("window_mode")
        legacy = MacroProfile.from_dict(old)
        legacy_events = _job_events(events, engine.record_point(adapter.window, "기존 기록 크기", 250, legacy))
        checks["legacy_recorded_fit_prepared"] = (legacy.window_mode == "recorded" and adapter.prepared_modes[-1] == "recorded"
            and any(event["type"] == "recorded" and event["window_mode"] == "recorded" for event in legacy_events)
            and len(adapter.sent) == count)
        checks["worker_idle"] = not engine.is_busy()
        _require(all(checks.values()), "A macro fixture check did not pass")
        result.update(ok=True, recorded_points=1, fake_clicks_sent=count, profile_file=profile_path.name)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        engine.close()
        checks["worker_closed"] = not engine.is_busy()
        result["ok"] = bool(result["ok"] and checks["worker_closed"])
        (output / "runtime_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
