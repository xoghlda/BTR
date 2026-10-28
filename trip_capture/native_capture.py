"""User-selected capture of browser, PDF viewer, and receipt output windows."""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
import uuid
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from .platformpaths import app_data_dir

HIPASS_URL = "https://www.hipass.co.kr/usepculr/InitUsePculrTabSearch.do"
KORAIL_URL = "https://www.korail.com/ticket/mypage/ticketInfo/receipt"


@dataclass(frozen=True)
class NativeBrowserWindow:
    hwnd: int
    title: str
    browser: str


def _apis():
    if sys.platform != "win32":
        raise RuntimeError("일반 브라우저 창 저장은 Windows에서 사용할 수 있습니다.")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsIconic.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return user, kernel


def _application_name(hwnd: int, user, kernel) -> str | None:
    process_id = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process = kernel.OpenProcess(0x1000, False, process_id.value)
    if not process:
        return None
    try:
        length = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(length)):
            return None
        name = Path(executable.value).name.lower()
        return {"chrome.exe": "Chrome", "msedge.exe": "Edge"}.get(name, Path(name).stem)
    finally:
        kernel.CloseHandle(process)


def _browser_name(hwnd: int, user, kernel) -> str | None:
    application = _application_name(hwnd, user, kernel)
    return application if application in ("Chrome", "Edge") else None


def list_browser_windows(include_other_windows: bool = False) -> list[NativeBrowserWindow]:
    user, kernel = _apis()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _parameter):
        if not user.IsWindowVisible(hwnd):
            return True
        length = user.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        process_id = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == os.getpid():
            return True
        window_class = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, window_class, len(window_class))
        if window_class.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return True
        application = _application_name(hwnd, user, kernel) if include_other_windows else _browser_name(hwnd, user, kernel)
        if application and application.casefold() not in {
            "explorer", "dwm", "shellexperiencehost", "startmenuexperiencehost", "searchhost", "textinputhost", "lockapp"
        }:
            title = ctypes.create_unicode_buffer(min(length + 1, 2048))
            user.GetWindowTextW(hwnd, title, len(title))
            if title.value.strip():
                windows.append(NativeBrowserWindow(int(hwnd), title.value, application))
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    if not user.EnumWindows(collect, 0):
        raise RuntimeError("출력창 목록을 가져오지 못했습니다. 다시 새로고침해 주세요.")
    return sorted(windows, key=lambda window: (window.browser, window.title.casefold(), window.hwnd))


def activate_browser_window(window: NativeBrowserWindow) -> None:
    user, kernel = _apis()
    if not user.IsWindow(window.hwnd) or _application_name(window.hwnd, user, kernel) != window.browser:
        raise RuntimeError("선택한 출력창이 닫혔습니다. 목록을 새로고침하고 다시 선택하세요.")
    if user.IsIconic(window.hwnd):
        user.ShowWindow(window.hwnd, 9)  # SW_RESTORE only when minimized
    user.SetForegroundWindow(window.hwnd)


def validate_bounds(bounds: tuple[int, int, int, int], screen: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        raise RuntimeError("선택한 창의 크기를 확인할 수 없습니다.")
    if left < screen[0] or top < screen[1] or right > screen[2] or bottom > screen[3]:
        raise RuntimeError("선택한 창이 화면 밖에 있습니다. 창을 화면 안으로 이동하거나 최대화한 뒤 다시 저장하세요.")


def save_visible_browser_window(window: NativeBrowserWindow, destination: Path) -> None:
    """Save visible screen pixels only after verifying the chosen foreground HWND."""
    from PIL import ImageGrab

    user, kernel = _apis()
    if not user.IsWindow(window.hwnd) or _application_name(window.hwnd, user, kernel) != window.browser:
        raise RuntimeError("선택한 출력창을 찾을 수 없습니다. 창 목록을 새로고침해 주세요.")
    if user.GetForegroundWindow() != window.hwnd:
        raise RuntimeError("선택한 출력창이 맨 앞에 오지 않았습니다. 해당 창을 한 번 클릭한 뒤 다시 저장하세요.")

    old_dpi = None
    set_dpi = getattr(user, "SetThreadDpiAwarenessContext", None)
    if set_dpi is not None:
        set_dpi.argtypes = [wintypes.HANDLE]
        set_dpi.restype = wintypes.HANDLE
        old_dpi = set_dpi(ctypes.c_void_p(-4))  # Per-monitor physical pixel coordinates
    try:
        rectangle = wintypes.RECT()
        found = False
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            dwm.DwmGetWindowAttribute.restype = ctypes.c_long
            found = dwm.DwmGetWindowAttribute(window.hwnd, 9, ctypes.byref(rectangle), ctypes.sizeof(rectangle)) == 0
        except (OSError, AttributeError):
            pass
        if not found and not user.GetWindowRect(window.hwnd, ctypes.byref(rectangle)):
            raise RuntimeError("브라우저 창 위치를 확인할 수 없습니다.")
        bounds = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
        screen_left, screen_top = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        screen = (screen_left, screen_top, screen_left + user.GetSystemMetrics(78), screen_top + user.GetSystemMetrics(79))
        validate_bounds(bounds, screen)
        if user.GetForegroundWindow() != window.hwnd:
            raise RuntimeError("저장 직전에 다른 창이 선택됐습니다. 다시 저장해 주세요.")
        try:
            screenshot = ImageGrab.grab(bbox=bounds, all_screens=True)
            try:
                if user.GetForegroundWindow() != window.hwnd or _application_name(window.hwnd, user, kernel) != window.browser:
                    raise RuntimeError("저장 도중 다른 창으로 전환됐습니다. 영수증 창을 선택한 뒤 다시 저장해 주세요.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                screenshot.save(destination, format="PNG")
            finally:
                screenshot.close()
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("화면 저장에 실패했습니다. 잠금 화면을 해제하고 브라우저 창을 확인해 주세요.") from exc
    finally:
        if set_dpi is not None and old_dpi:
            set_dpi(old_dpi)


class NativeCaptureDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, on_capture: Callable[[Path, str], None], *, site: str = "hipass") -> None:
        super().__init__(parent)
        self.parent_window = parent
        self.on_capture = on_capture
        self.site = site if site in ("hipass", "korail") else "hipass"
        self.site_url = KORAIL_URL if self.site == "korail" else HIPASS_URL
        label = "코레일" if self.site == "korail" else "하이패스"
        self._windows: dict[str, NativeBrowserWindow] = {}
        self._busy = False
        self.title(f"{label} 영수증 출력창 저장")
        self.geometry("760x410")
        self.minsize(700, 390)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._dismiss)
        self.selection = tk.StringVar()
        self.include_other_windows = tk.BooleanVar(value=True)
        self.message = tk.StringVar(value="브라우저·PDF 뷰어·별도 출력 프로그램에서 영수증을 준비한 후 해당 창을 직접 선택하세요.")
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="영수증이 열린 출력창을 선택하세요", font=("맑은 고딕", 13, "bold")).pack(anchor="w")
        ttk.Label(body, textvariable=self.message, wraplength=690).pack(anchor="w", pady=(8, 10))
        controls = ttk.Frame(body)
        controls.pack(fill="x")
        ttk.Button(controls, text=f"일반 브라우저로 {label} 열기", command=self._open_browser).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="창 목록 새로고침", command=self._refresh).pack(side="left")
        ttk.Checkbutton(body, text="PDF 뷰어·별도 출력창 포함", variable=self.include_other_windows, command=self._refresh).pack(anchor="w", pady=(9, 0))
        self.combo = ttk.Combobox(body, textvariable=self.selection, state="readonly")
        self.combo.pack(fill="x", pady=(12, 8))
        self.combo.bind("<<ComboboxSelected>>", lambda _event: self._sync_button())
        ttk.Label(body, text="선택한 창에서 보이는 영역만 저장됩니다. 로그인·비밀번호 입력 화면 대신 영수증 화면을 선택하세요.", wraplength=690).pack(anchor="w")
        ttk.Label(body, text="PDF 파일이 있으면 이 창을 닫고 ‘영수증 PDF 가져오기’를 사용하세요. 원본과 모든 페이지가 보고서에 저장됩니다.", wraplength=690).pack(anchor="w", pady=(6, 0))
        ttk.Label(body, text="저장 버튼을 누르면 이 창이 숨겨집니다. 4초 안에 선택한 영수증 창을 직접 클릭하세요.", wraplength=690).pack(anchor="w", pady=(6, 0))
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(14, 0))
        self.capture_button = ttk.Button(footer, text="4초 후 선택한 영수증 창 저장", command=self._capture, state="disabled")
        self.capture_button.pack(side="right")
        ttk.Button(footer, text="닫기", command=self._dismiss).pack(side="right", padx=(0, 8))
        self._refresh()
        self.grab_set()

    def _open_browser(self) -> None:
        webbrowser.open(self.site_url)
        self.message.set("열린 브라우저에서 직접 로그인·인증하고 영수증을 준비한 뒤, 목록을 새로고침하세요.")

    def _refresh(self) -> None:
        selected = self._windows.get(self.selection.get())
        try:
            windows = list_browser_windows(include_other_windows=self.include_other_windows.get())
        except Exception as exc:
            self.message.set(str(exc))
            windows = []
        self._windows = {
            f"{number}. [{window.browser}] {window.title[:100]}": window
            for number, window in enumerate(windows, 1)
        }
        self.combo.configure(values=list(self._windows))
        self.selection.set(next((label for label, window in self._windows.items() if selected and selected.hwnd == window.hwnd), ""))
        if not windows:
            self.message.set("선택할 출력창이 없습니다. 영수증 창을 열고 목록을 새로고침하세요. PDF 파일은 기본 화면에서 가져올 수 있습니다.")
        self._sync_button()

    def _sync_button(self) -> None:
        self.capture_button.state(["!disabled"] if not self._busy and self.selection.get() in self._windows else ["disabled"])

    def _capture(self) -> None:
        window = self._windows.get(self.selection.get())
        if not window or self._busy:
            return
        self._busy = True
        self._sync_button()
        self.grab_release()
        self.withdraw()
        self.parent_window.withdraw()
        self.after(700, lambda: self._activate(window))

    def _activate(self, window: NativeBrowserWindow) -> None:
        try:
            activate_browser_window(window)
        except Exception as exc:
            self._failed(str(exc))
            return
        # Windows may reject a foreground request. Give the user time to click
        # the selected normal browser instead of relying on that request.
        self.after(3000, lambda: self._save(window))

    def _save(self, window: NativeBrowserWindow) -> None:
        path = None
        try:
            path = app_data_dir() / "staging" / f"native-{uuid.uuid4().hex}.png"
            save_visible_browser_window(window, path)
        except Exception as exc:
            if path is not None:
                path.unlink(missing_ok=True)
            self._failed(str(exc))
            return
        self.parent_window.deiconify()
        self.destroy()
        try:
            self.on_capture(path, window.title)
        except Exception:
            path.unlink(missing_ok=True)
            messagebox.showerror("영수증 저장", "저장한 화면을 보고서에 전달하지 못했습니다. 다시 시도해 주세요.", parent=self.parent_window)

    def _failed(self, message: str) -> None:
        self._busy = False
        self.parent_window.deiconify()
        self.deiconify()
        self.lift()
        self.grab_set()
        self.message.set(message)
        self._sync_button()

    def _dismiss(self) -> None:
        if not self._busy:
            self.grab_release()
            self.destroy()
