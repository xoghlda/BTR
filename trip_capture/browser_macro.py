"""Explicit, window-relative left-click macros for a user-selected browser.

Recording samples a pointer position after four seconds; it does not hook
mouse/keyboard input. Profiles contain only browser, geometry and click points.
The adapter boundary permits tests without invoking any Windows input API.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import Protocol
import uuid

from .native_capture import NativeBrowserWindow, list_browser_windows

MAX_PROFILE_BYTES = 64 * 1024
MAX_STEPS = 30
MAX_TOTAL_WAIT_MS = 300_000
MIN_STEP_WAIT_MS = 200
MAX_STEP_WAIT_MS = 30_000
COUNTDOWN_SECONDS = 4
MAX_RUNTIME_SECONDS = 330
POLL_SECONDS = 0.05
WINDOW_MODES = ("maximize", "recorded")
BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "iexplore.exe": "Internet Explorer"}


class MacroError(ValueError):
    """Safe, user-facing validation or runtime error."""


class _Stopped(MacroError):
    pass


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise MacroError(f"{label} 값이 허용 범위를 벗어났습니다.")
    return value


def _name(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 80
            or any(ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise MacroError("클릭 이름은 줄바꿈 없이 1~80자로 입력해 주세요.")
    return value.strip()


def _keys(value, expected: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise MacroError(f"{label} 형식이 올바르지 않거나 지원하지 않는 항목이 있습니다.")


def _browser(value: str) -> str:
    if not isinstance(value, str) or value not in BROWSERS.values():
        raise MacroError("Chrome, Edge 또는 Internet Explorer 창을 선택해 주세요.")
    return value


def _window_mode(value: str) -> str:
    if not isinstance(value, str) or value not in WINDOW_MODES:
        raise MacroError("창 준비 방식은 최대화 또는 기록한 크기만 사용할 수 있습니다.")
    return value


@dataclass(frozen=True)
class Geometry:
    width: int
    height: int
    dpi: int

    def __post_init__(self):
        _integer(self.width, 1, 32768, "창 너비")
        _integer(self.height, 1, 32768, "창 높이")
        _integer(self.dpi, 48, 768, "화면 배율")


@dataclass(frozen=True)
class ClickStep:
    name: str
    x: int
    y: int
    wait_ms: int

    def __post_init__(self):
        object.__setattr__(self, "name", _name(self.name))
        _integer(self.x, 0, 32767, "클릭 가로 위치")
        _integer(self.y, 0, 32767, "클릭 세로 위치")
        _integer(self.wait_ms, MIN_STEP_WAIT_MS, MAX_STEP_WAIT_MS, "클릭 후 대기 시간")


@dataclass(frozen=True)
class MacroProfile:
    browser: str
    geometry: Geometry
    steps: tuple[ClickStep, ...]
    window_mode: str = "maximize"

    def __post_init__(self):
        _browser(self.browser)
        _window_mode(self.window_mode)
        if not isinstance(self.geometry, Geometry) or type(self.steps) is not tuple:
            raise MacroError("매크로 창 정보 또는 클릭 목록 형식이 올바르지 않습니다.")
        if len(self.steps) > MAX_STEPS or not all(isinstance(step, ClickStep) for step in self.steps):
            raise MacroError(f"클릭 지점은 최대 {MAX_STEPS}개까지 기록할 수 있습니다.")
        if sum(step.wait_ms for step in self.steps) > MAX_TOTAL_WAIT_MS:
            raise MacroError("전체 클릭 대기 시간은 300초 이하로 설정해 주세요.")
        if any(step.x >= self.geometry.width or step.y >= self.geometry.height for step in self.steps):
            raise MacroError("클릭 지점이 기록한 창 영역을 벗어났습니다.")

    def to_dict(self) -> dict:
        return {
            "version": 2, "browser": self.browser, "window_mode": self.window_mode,
            "geometry": {"width": self.geometry.width, "height": self.geometry.height, "dpi": self.geometry.dpi},
            "steps": [{"name": step.name, "x": step.x, "y": step.y, "wait_ms": step.wait_ms} for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: dict) -> "MacroProfile":
        if type(value) is not dict or type(value.get("version")) is not int or value["version"] not in (1, 2):
            raise MacroError("지원하지 않는 매크로 파일 버전입니다.")
        expected = {"version", "browser", "geometry", "steps"}
        if value["version"] == 2:
            expected.add("window_mode")
        _keys(value, expected, "매크로 파일")
        geometry = value["geometry"]
        _keys(geometry, {"width", "height", "dpi"}, "창 정보")
        steps = value["steps"]
        if type(steps) is not list or len(steps) > MAX_STEPS:
            raise MacroError(f"클릭 목록은 최대 {MAX_STEPS}개인 배열이어야 합니다.")
        for step in steps:
            _keys(step, {"name", "x", "y", "wait_ms"}, "클릭 지점")
        return cls(value["browser"], Geometry(**geometry), tuple(ClickStep(**step) for step in steps),
                   value["window_mode"] if value["version"] == 2 else "recorded")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MacroError("중복된 항목이 있는 매크로 파일은 사용할 수 없습니다.")
        result[key] = value
    return result


def load_profile(path: Path) -> MacroProfile:
    try:
        with Path(path).open("rb") as source:
            data = source.read(MAX_PROFILE_BYTES + 1)
        if len(data) > MAX_PROFILE_BYTES:
            raise MacroError("매크로 파일은 64 KiB 이하만 사용할 수 있습니다.")
        return MacroProfile.from_dict(json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object))
    except MacroError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise MacroError("매크로 파일을 읽지 못했습니다. 지원하는 JSON 파일인지 확인해 주세요.") from None


def save_profile(path: Path, profile: MacroProfile) -> None:
    if not isinstance(profile, MacroProfile):
        raise MacroError("저장할 매크로 정보가 올바르지 않습니다.")
    data = (json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_PROFILE_BYTES:
        raise MacroError("매크로 파일은 64 KiB 이하로 저장해 주세요.")
    temporary = None
    try:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        os.replace(temporary, destination)
    except OSError:
        raise MacroError("매크로 파일을 저장하지 못했습니다. 폴더 권한을 확인해 주세요.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    created: int
    executable: str


@dataclass(frozen=True)
class WindowSnapshot:
    identity: WindowIdentity
    geometry: Geometry
    left: int
    top: int
    visible: bool = True
    minimized: bool = False
    foreground: bool = True


class MacroAdapter(Protocol):
    def list_windows(self) -> list[NativeBrowserWindow]: ...
    def bind(self, window: NativeBrowserWindow) -> WindowIdentity: ...
    def prepare_window(self, identity: WindowIdentity, mode: str, expected_geometry: Geometry | None = None,
                       *, stopped: threading.Event | None = None) -> WindowSnapshot: ...
    def snapshot(self, identity: WindowIdentity) -> WindowSnapshot: ...
    def cursor_position(self) -> tuple[int, int]: ...
    def escape_pressed(self) -> bool: ...
    def left_click(self, identity: WindowIdentity, geometry: Geometry, x: int, y: int) -> None: ...
    def monotonic(self) -> float: ...
    def wait(self, seconds: float, stopped: threading.Event) -> None: ...


def _validate_snapshot(snapshot: WindowSnapshot, identity: WindowIdentity, geometry: Geometry | None, *, require_foreground: bool = True):
    if snapshot.identity != identity:
        raise MacroError("선택한 창의 프로세스가 변경됐습니다. 창을 다시 선택해 주세요.")
    if not snapshot.visible or snapshot.minimized:
        raise MacroError("선택한 창이 숨겨졌거나 최소화됐습니다. 창을 표시한 뒤 다시 시도하세요.")
    if require_foreground and not snapshot.foreground:
        raise MacroError("선택한 브라우저가 맨 앞에 없습니다. 해당 창을 클릭한 뒤 다시 시도하세요.")
    if geometry is not None and snapshot.geometry != geometry:
        raise MacroError("기록할 때와 창 크기 또는 화면 배율이 다릅니다. 크기·배율을 맞추거나 다시 기록해 주세요.")


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class Win32Adapter:
    """Lazy Win32 access; construction and imports do not enumerate or send input."""

    def __init__(self):
        self._user = None
        self._kernel = None

    def _apis(self):
        if self._user is None:
            if os.name != "nt":
                raise MacroError("브라우저 클릭 매크로는 Windows에서 사용할 수 있습니다.")
            user = ctypes.WinDLL("user32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for name in ("IsWindow", "IsWindowVisible", "IsIconic", "IsZoomed"):
                getattr(user, name).argtypes = [wintypes.HWND]
                getattr(user, name).restype = wintypes.BOOL
            user.GetForegroundWindow.restype = wintypes.HWND
            user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user.ShowWindow.restype = wintypes.BOOL
            if hasattr(user, "ShowWindowAsync"):
                user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
                user.ShowWindowAsync.restype = wintypes.BOOL
            user.SetForegroundWindow.argtypes = [wintypes.HWND]
            user.SetForegroundWindow.restype = wintypes.BOOL
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user.SetWindowPos.restype = wintypes.BOOL
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
            user.GetMonitorInfoW.restype = wintypes.BOOL
            user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            user.WindowFromPoint.argtypes = [wintypes.POINT]
            user.WindowFromPoint.restype = wintypes.HWND
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            user.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user.GetAsyncKeyState.restype = ctypes.c_short
            user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
            user.SendInput.restype = wintypes.UINT
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self._user, self._kernel = user, kernel
        return self._user, self._kernel

    @contextmanager
    def _physical_pixels(self):
        user, _ = self._apis()
        setter = getattr(user, "SetThreadDpiAwarenessContext", None)
        previous = None
        if setter is not None:
            setter.argtypes = [wintypes.HANDLE]
            setter.restype = wintypes.HANDLE
            previous = setter(ctypes.c_void_p(-4))
            if not previous:
                raise MacroError("DPI 물리 좌표 모드를 설정하지 못했습니다. 화면 배율을 확인하고 다시 시도해 주세요.")
        try:
            yield user
        finally:
            if setter is not None and previous:
                setter(previous)

    def list_windows(self) -> list[NativeBrowserWindow]:
        labels = {"chrome": "Chrome", "edge": "Edge", "iexplore": "Internet Explorer"}
        return [NativeBrowserWindow(window.hwnd, window.title, labels[window.browser.casefold()])
                for window in list_browser_windows(include_other_windows=True)
                if window.browser.casefold() in labels]

    def _identity(self, hwnd: int) -> WindowIdentity:
        user, kernel = self._apis()
        if not user.IsWindow(hwnd):
            raise MacroError("선택한 창이 닫혔습니다. 브라우저 창을 다시 선택해 주세요.")
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            raise MacroError("선택한 브라우저 프로세스를 확인하지 못했습니다.")
        try:
            length = wintypes.DWORD(32768)
            executable = ctypes.create_unicode_buffer(length.value)
            times = [wintypes.FILETIME() for _ in range(4)]
            if (not kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(length))
                    or not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))):
                raise MacroError("브라우저 실행 정보를 확인하지 못했습니다.")
            path = executable.value.casefold()
            if Path(path).name not in BROWSERS:
                raise MacroError("Chrome, Edge 또는 Internet Explorer 창만 선택할 수 있습니다.")
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return WindowIdentity(hwnd, pid.value, created, path)
        finally:
            kernel.CloseHandle(handle)

    def bind(self, window: NativeBrowserWindow) -> WindowIdentity:
        identity = self._identity(window.hwnd)
        if BROWSERS[Path(identity.executable).name] != _browser(window.browser):
            raise MacroError("선택한 창의 브라우저 종류가 변경됐습니다.")
        return identity

    def snapshot(self, identity: WindowIdentity) -> WindowSnapshot:
        actual = self._identity(identity.hwnd)
        with self._physical_pixels() as user:
            rectangle = wintypes.RECT()
            origin = wintypes.POINT(0, 0)
            if not user.GetClientRect(identity.hwnd, ctypes.byref(rectangle)) or not user.ClientToScreen(identity.hwnd, ctypes.byref(origin)):
                raise MacroError("브라우저의 화면 위치를 확인하지 못했습니다.")
            getter = getattr(user, "GetDpiForWindow", None)
            if getter is None:
                raise MacroError("창 배율을 확인할 수 없는 Windows 버전입니다.")
            getter.argtypes = [wintypes.HWND]
            getter.restype = wintypes.UINT
            geometry = Geometry(rectangle.right - rectangle.left, rectangle.bottom - rectangle.top, int(getter(identity.hwnd)))
            return WindowSnapshot(actual, geometry, origin.x, origin.y, bool(user.IsWindowVisible(identity.hwnd)),
                                  bool(user.IsIconic(identity.hwnd)), user.GetForegroundWindow() == identity.hwnd)

    def _prepare_guard(self, identity: WindowIdentity, stopped: threading.Event, deadline: float):
        if stopped.is_set():
            raise _Stopped("사용자가 창 준비를 중지했습니다.")
        if self.escape_pressed():
            raise _Stopped("Esc 키로 창 준비를 중지했습니다.")
        x, y = self.cursor_position()
        if 0 <= x <= 3 and 0 <= y <= 3:
            raise _Stopped("마우스를 주 모니터 왼쪽 위로 이동해 창 준비를 중지했습니다.")
        if self.monotonic() >= deadline:
            raise MacroError("창 크기가 제한 시간 안에 안정되지 않았습니다. 브라우저 응답을 확인하고 다시 시도하세요.")
        if self._identity(identity.hwnd) != identity:
            raise MacroError("선택한 창의 프로세스가 변경됐습니다. 창을 다시 선택해 주세요.")

    def _stable_window(self, identity: WindowIdentity, stopped: threading.Event, deadline: float,
                       zoomed: bool | None = None, expected_geometry: Geometry | None = None,
                       within_work: tuple[int, int, int, int] | None = None) -> WindowSnapshot:
        previous = None
        repeats = 0
        user, _ = self._apis()
        while True:
            self._prepare_guard(identity, stopped, deadline)
            if user.IsIconic(identity.hwnd) or not user.IsWindowVisible(identity.hwnd):
                previous = None
                repeats = 0
                self.wait(POLL_SECONDS, stopped)
                continue
            snapshot = self.snapshot(identity)
            _validate_snapshot(snapshot, identity, None, require_foreground=False)
            current_zoomed = bool(user.IsZoomed(identity.hwnd))
            shape = (snapshot.geometry, snapshot.left, snapshot.top, current_zoomed)
            repeats = repeats + 1 if shape == previous else 1
            previous = shape
            if (repeats >= 3 and (zoomed is None or current_zoomed == zoomed)
                    and (expected_geometry is None or snapshot.geometry == expected_geometry)
                    and (within_work is None or self._inside_work(snapshot, within_work))):
                return snapshot
            self.wait(POLL_SECONDS, stopped)

    def _work_area(self, identity: WindowIdentity) -> tuple[int, int, int, int]:
        user, _ = self._apis()
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        monitor = user.MonitorFromWindow(identity.hwnd, 2)
        if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise MacroError("선택한 창이 있는 모니터의 작업 영역을 확인하지 못했습니다.")
        bounds = (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)
        if not (0 < bounds[2] - bounds[0] <= 32768 and 0 < bounds[3] - bounds[1] <= 32768):
            raise MacroError("모니터 작업 영역 크기가 올바르지 않습니다.")
        return bounds

    @staticmethod
    def _inside_work(snapshot: WindowSnapshot, bounds: tuple[int, int, int, int]) -> bool:
        return (snapshot.left >= bounds[0] and snapshot.top >= bounds[1]
                and snapshot.left + snapshot.geometry.width <= bounds[2]
                and snapshot.top + snapshot.geometry.height <= bounds[3])

    def prepare_window(self, identity: WindowIdentity, mode: str, expected_geometry: Geometry | None = None,
                       *, stopped: threading.Event | None = None) -> WindowSnapshot:
        """Normalize only the selected window; retain exact client pixels/DPI."""
        mode = _window_mode(mode)
        if expected_geometry is not None and not isinstance(expected_geometry, Geometry):
            raise MacroError("기록한 창 크기 정보가 올바르지 않습니다.")
        stopped = stopped if stopped is not None else threading.Event()
        deadline = self.monotonic() + 3.0
        with self._physical_pixels() as user:
            show = getattr(user, "ShowWindowAsync", None) or user.ShowWindow
            def change_visibility(command: int):
                self._prepare_guard(identity, stopped, deadline)
                show(identity.hwnd, command)  # Zero is not interpreted as failure; verify snapshots.
            def maximize():
                work = self._work_area(identity)
                change_visibility(3)
                return self._stable_window(identity, stopped, deadline, zoomed=True,
                                           expected_geometry=expected_geometry, within_work=work)
            self._prepare_guard(identity, stopped, deadline)
            if user.IsIconic(identity.hwnd):
                change_visibility(9)
            if mode == "maximize":
                snapshot = maximize()
            else:
                snapshot = self._stable_window(identity, stopped, deadline)
                if expected_geometry is not None:
                    if snapshot.geometry.dpi != expected_geometry.dpi:
                        raise MacroError("기록한 모니터와 화면 배율이 다릅니다. 같은 배율의 모니터에서 다시 시도하세요.")
                    work = self._work_area(identity)
                    if snapshot.geometry != expected_geometry or not self._inside_work(snapshot, work):
                        if user.IsZoomed(identity.hwnd):
                            change_visibility(9)
                            snapshot = self._stable_window(identity, stopped, deadline, zoomed=False)
                        for _ in range(3):
                            if snapshot.geometry.dpi != expected_geometry.dpi:
                                raise MacroError("창을 준비하는 동안 화면 배율이 변경됐습니다.")
                            rectangle = wintypes.RECT()
                            self._prepare_guard(identity, stopped, deadline)
                            if not user.GetWindowRect(identity.hwnd, ctypes.byref(rectangle)):
                                raise MacroError("선택한 창의 외곽 크기를 확인하지 못했습니다.")
                            frame_width = rectangle.right - rectangle.left - snapshot.geometry.width
                            frame_height = rectangle.bottom - rectangle.top - snapshot.geometry.height
                            if frame_width < 0 or frame_height < 0:
                                raise MacroError("창의 실제 픽셀 경계를 확인하지 못했습니다.")
                            width, height = expected_geometry.width + frame_width, expected_geometry.height + frame_height
                            if width > work[2] - work[0] or height > work[3] - work[1]:
                                snapshot = maximize()  # Old maximized v1 recordings can fit only this exact shape.
                                break
                            left = min(max(rectangle.left, work[0]), work[2] - width)
                            top = min(max(rectangle.top, work[1]), work[3] - height)
                            self._prepare_guard(identity, stopped, deadline)
                            flags = 0x0004 | 0x0010 | 0x0200 | 0x4000  # NOZORDER/NOACTIVATE/NOOWNERZORDER/ASYNC
                            if not user.SetWindowPos(identity.hwnd, None, left, top, width, height, flags):
                                raise MacroError("기록한 크기로 창을 맞추지 못했습니다.")
                            snapshot = self._stable_window(identity, stopped, deadline, zoomed=False,
                                                           expected_geometry=expected_geometry, within_work=work)
                            if snapshot.geometry == expected_geometry:
                                break
            _validate_snapshot(snapshot, identity, expected_geometry, require_foreground=False)
            self._prepare_guard(identity, stopped, deadline)
            if not self._inside_work(snapshot, self._work_area(identity)):
                raise MacroError("선택한 창을 현재 모니터의 작업 영역 안에 맞추지 못했습니다.")
            self._prepare_guard(identity, stopped, deadline)
            user.SetForegroundWindow(identity.hwnd)  # OS denial is normal; user has the following countdown.
            return snapshot

    def cursor_position(self) -> tuple[int, int]:
        with self._physical_pixels() as user:
            point = wintypes.POINT()
            if not user.GetCursorPos(ctypes.byref(point)):
                raise MacroError("마우스 위치를 확인하지 못했습니다.")
            return point.x, point.y

    def escape_pressed(self) -> bool:
        user, _ = self._apis()
        return bool(user.GetAsyncKeyState(0x1B) & 0x8000)

    def left_click(self, identity: WindowIdentity, geometry: Geometry, x: int, y: int) -> None:
        # Revalidate again inside the adapter immediately before injection.
        snapshot = self.snapshot(identity)
        _validate_snapshot(snapshot, identity, geometry)
        _integer(x, 0, geometry.width - 1, "클릭 가로 위치")
        _integer(y, 0, geometry.height - 1, "클릭 세로 위치")
        screen_x, screen_y = snapshot.left + x, snapshot.top + y
        with self._physical_pixels() as user:
            left, top, width, height = (user.GetSystemMetrics(value) for value in (76, 77, 78, 79))
            if width < 2 or height < 2 or not (left <= screen_x < left + width and top <= screen_y < top + height):
                raise MacroError("클릭 지점이 실제 화면 범위를 벗어났습니다.")
            if user.GetForegroundWindow() != identity.hwnd:
                raise MacroError("클릭 직전에 다른 창으로 전환됐습니다. 재생을 중단합니다.")
            hit = user.WindowFromPoint(wintypes.POINT(screen_x, screen_y))
            if not hit or user.GetAncestor(hit, 2) != identity.hwnd:
                raise MacroError("클릭 지점을 다른 창이 가리고 있습니다. 가리는 창을 닫거나 이동한 뒤 다시 시도하세요.")
            if any(user.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x01)):
                raise MacroError("Shift·Ctrl·Alt·Windows 키와 마우스 왼쪽 버튼을 놓은 뒤 다시 재생해 주세요.")
            absolute_x = round((screen_x - left) * 65535 / (width - 1))
            absolute_y = round((screen_y - top) * 65535 / (height - 1))
            inputs = (_Input * 3)()
            inputs[0].mi = _MouseInput(absolute_x, absolute_y, 0, 0x0001 | 0x8000 | 0x4000, 0, 0)
            inputs[1].mi.dwFlags = 0x0002
            inputs[2].mi.dwFlags = 0x0004
            sent = user.SendInput(3, inputs, ctypes.sizeof(_Input))
            if sent != 3:
                if sent >= 2:
                    release = _Input()
                    release.mi.dwFlags = 0x0004
                    user.SendInput(1, ctypes.byref(release), ctypes.sizeof(_Input))
                raise MacroError("클릭 입력을 전송하지 못했습니다. 브라우저 창과 실행 권한을 확인해 주세요.")

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def wait(seconds: float, stopped: threading.Event) -> None:
        stopped.wait(seconds)


class BrowserMacro:
    def __init__(self, events: queue.Queue, adapter: MacroAdapter | None = None):
        self.events = events
        self.adapter = adapter if adapter is not None else Win32Adapter()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._job_id: str | None = None
        self._stop = threading.Event()
        self._closed = False

    def list_windows(self) -> list[NativeBrowserWindow]:
        if self._closed:
            raise MacroError("매크로 창이 닫혔습니다.")
        return self.adapter.list_windows()

    def is_busy(self) -> bool:
        with self._lock:
            return self._job_id is not None

    def _emit(self, job_id: str, kind: str, **values):
        self.events.put({"type": kind, "job_id": job_id, **values})

    def _start(self, operation):
        with self._lock:
            if self._closed:
                raise MacroError("매크로 창이 닫혔습니다.")
            if self._job_id is not None:
                raise MacroError("기록 또는 재생 중입니다. 먼저 중지하거나 완료될 때까지 기다려 주세요.")
            job_id = uuid.uuid4().hex
            stopped = threading.Event()
            self._stop = stopped
            self._job_id = job_id
            def run():
                try:
                    operation(job_id, stopped)
                except _Stopped as error:
                    self._emit(job_id, "stopped", message=str(error))
                except MacroError as error:
                    self._emit(job_id, "error", message=str(error))
                except Exception:
                    self._emit(job_id, "error", message="매크로 작업을 진행하지 못했습니다. 선택한 창을 확인하고 다시 시도해 주세요.")
                finally:
                    with self._lock:
                        if self._job_id == job_id:
                            self._job_id = None
                    self._emit(job_id, "idle")
            self._thread = threading.Thread(target=run, name="browser-click-macro", daemon=True)
            self._thread.start()
            return job_id

    def _check_stop(self, stopped: threading.Event, deadline: float):
        if stopped.is_set() or self._closed:
            raise _Stopped("사용자가 매크로를 중지했습니다.")
        if self.adapter.escape_pressed():
            raise _Stopped("Esc 키로 매크로를 중지했습니다.")
        x, y = self.adapter.cursor_position()
        if 0 <= x <= 3 and 0 <= y <= 3:
            raise _Stopped("마우스를 주 모니터 왼쪽 위로 이동해 매크로를 중지했습니다.")
        if self.adapter.monotonic() >= deadline:
            raise _Stopped("매크로 실행 제한 시간이 지나 중지했습니다.")

    def _pause(self, seconds: float, stopped: threading.Event, deadline: float):
        until = self.adapter.monotonic() + seconds
        while True:
            self._check_stop(stopped, deadline)
            remaining = until - self.adapter.monotonic()
            if remaining <= 0:
                return
            self.adapter.wait(min(POLL_SECONDS, remaining), stopped)

    def _countdown(self, job_id: str, mode: str, stopped: threading.Event, deadline: float):
        for remaining in range(COUNTDOWN_SECONDS, 0, -1):
            self._check_stop(stopped, deadline)
            self._emit(job_id, "countdown", mode=mode, remaining=remaining)
            self._pause(1.0, stopped, deadline)

    def record_point(self, window: NativeBrowserWindow, name: str, wait_ms: int, profile: MacroProfile | None = None,
                     *, window_mode: str | None = None) -> str:
        label = _name(name)
        wait_ms = _integer(wait_ms, MIN_STEP_WAIT_MS, MAX_STEP_WAIT_MS, "클릭 후 대기 시간")
        browser = _browser(window.browser)
        if profile is not None and not isinstance(profile, MacroProfile):
            raise MacroError("기존 매크로 정보가 올바르지 않습니다.")
        mode = _window_mode(window_mode if window_mode is not None else (profile.window_mode if profile else "maximize"))
        if profile is not None:
            if not isinstance(profile, MacroProfile) or profile.browser != browser:
                raise MacroError("같은 브라우저 종류의 매크로에만 클릭을 추가할 수 있습니다.")
            if mode != profile.window_mode:
                raise MacroError("기록된 지점의 창 준비 방식은 바꿀 수 없습니다. 새 매크로로 기록해 주세요.")
            if len(profile.steps) >= MAX_STEPS or sum(step.wait_ms for step in profile.steps) + wait_ms > MAX_TOTAL_WAIT_MS:
                raise MacroError("클릭 30개·전체 대기 300초 제한을 초과합니다.")
        def record(job_id, stopped):
            identity = self.adapter.bind(window)
            deadline = self.adapter.monotonic() + 15
            self._emit(job_id, "preparing", window_mode=mode)
            prepared = self.adapter.prepare_window(identity, mode, profile.geometry if profile else None, stopped=stopped)
            _validate_snapshot(prepared, identity, profile.geometry if profile else None, require_foreground=False)
            self._countdown(job_id, "recording", stopped, deadline)
            snapshot = self.adapter.snapshot(identity)
            _validate_snapshot(snapshot, identity, prepared.geometry)
            x, y = self.adapter.cursor_position()
            step = ClickStep(label, x - snapshot.left, y - snapshot.top, wait_ms)
            MacroProfile(browser, snapshot.geometry, (step,), mode)  # Check point is inside the client.
            self._check_stop(stopped, deadline)
            self._emit(job_id, "recorded", step=step, geometry=snapshot.geometry, browser=browser, window_mode=mode)
        return self._start(record)

    def play(self, window: NativeBrowserWindow, profile: MacroProfile) -> str:
        if not isinstance(profile, MacroProfile) or not profile.steps:
            raise MacroError("재생할 클릭 지점을 먼저 기록하거나 불러와 주세요.")
        if _browser(window.browser) != profile.browser:
            raise MacroError("기록한 매크로와 같은 종류의 브라우저를 선택해 주세요.")
        def playback(job_id, stopped):
            identity = self.adapter.bind(window)
            deadline = self.adapter.monotonic() + MAX_RUNTIME_SECONDS
            self._emit(job_id, "preparing", window_mode=profile.window_mode)
            prepared = self.adapter.prepare_window(identity, profile.window_mode, profile.geometry, stopped=stopped)
            _validate_snapshot(prepared, identity, profile.geometry, require_foreground=False)
            self._countdown(job_id, "playing", stopped, deadline)
            count = 0
            for index, step in enumerate(profile.steps, 1):
                self._check_stop(stopped, deadline)
                snapshot = self.adapter.snapshot(identity)
                _validate_snapshot(snapshot, identity, profile.geometry)
                self._check_stop(stopped, deadline)
                self.adapter.left_click(identity, profile.geometry, step.x, step.y)
                count += 1
                self._emit(job_id, "progress", index=index, total=len(profile.steps), name=step.name)
                self._pause(step.wait_ms / 1000, stopped, deadline)
            self._emit(job_id, "done", count=count)
        return self._start(playback)

    def cancel(self) -> None:
        with self._lock:
            self._stop.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
