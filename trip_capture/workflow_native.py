"""Guarded keyboard input and read-only browser-chrome URL inspection.

No clipboard, DOM editing, password extraction, security-keyboard changes or
browser connection happens at import/construction. Tests inject fake Win32/UIA.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import re
import threading
import time
from urllib.parse import urlsplit

from .browser_macro import Geometry, MacroError, Win32Adapter, WindowIdentity, _Input, _validate_snapshot


_KEYUP = 0x0002
_UNICODE = 0x0004
_EXTENDED = 0x0001
_MAX_TEXT = 4096
_CHUNK_UNITS = 16
_KEY_SETTLE_SECONDS = 0.15
_HELD_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x01)
_KEYS = {
    "ENTER": (0x0D,), "TAB": (0x09,), "SHIFT+TAB": (0x10, 0x09),
    "CTRL+A": (0x11, 0x41), "CTRL+P": (0x11, 0x50),
    "ESC": (0x1B,), "SPACE": (0x20,),
    "DOWN": (0x28,), "UP": (0x26,), "HOME": (0x24,), "END": (0x23,),
}
_EXTENDED_KEYS = {0x28, 0x26, 0x24, 0x23}
_UIA_EDIT = 50004
_UIA_DOCUMENT = 50030
_UIA_TOOLBAR = 50021
_UIA_VALUE_PATTERN = 10002
_ADDRESS_NAMES = {
    "address and search bar", "address bar", "address and search using bing",
    "search or enter web address", "search or enter address",
    "주소 및 검색창", "주소 및 검색 창", "주소창 및 검색창", "주소창 및 검색 창", "주소 및 검색 표시줄", "주소 표시줄",
    "주소 및 bing 검색", "검색 또는 웹 주소 입력", "검색어 또는 웹 주소 입력",
}
_ADDRESS_IDS = {"addresseditbox", "addressedit", "addressandsearchbar", "addressbar", "omnibox", "urlbar", "locationbar"}
_ADDRESS_CLASSES = {"chrome_omniboxview", "omniboxviewviews"}


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def _keyboard_packet(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    # The existing INPUT union is mouse-sized, hence also large enough for
    # KEYBDINPUT on both Windows x86 and x64. Retain its SendInput ABI exactly.
    event = _Input()
    event.type = 1  # INPUT_KEYBOARD
    keyboard = _KeyboardInput(vk, scan, flags, 0, 0)
    ctypes.memmove(ctypes.addressof(event.value), ctypes.byref(keyboard), ctypes.sizeof(keyboard))
    ctypes.memset(ctypes.byref(keyboard), 0, ctypes.sizeof(keyboard))
    return event


def _packet_fields(event: _Input) -> tuple[int, int, int]:
    value = _KeyboardInput.from_address(ctypes.addressof(event.value))
    return int(value.wVk), int(value.wScan), int(value.dwFlags)


def _rectangle(value) -> tuple[int, int, int, int]:
    if all(hasattr(value, name) for name in ("left", "top", "right", "bottom")):
        result = tuple(int(getattr(value, name)) for name in ("left", "top", "right", "bottom"))
    else:
        result = tuple(int(number) for number in value)
    if len(result) != 4 or result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("invalid control rectangle")
    return result


def _uia_get(element, name: str, checkpoint=None):
    if checkpoint is not None:
        checkpoint()
    value = getattr(element, name)
    if checkpoint is not None:
        checkpoint()
    return value


class WorkflowNativeAdapter(Win32Adapter):
    """Native keyboard adapter; engine separately enforces allowed domains."""

    def __init__(self, *, uia_factory=None):
        super().__init__()
        self._uia_factory = uia_factory

    @staticmethod
    def _cancelled(stopped) -> bool:
        if stopped is None:
            return False
        return bool(stopped() if callable(stopped) else stopped.is_set())

    def _guard(self, identity: WindowIdentity, geometry: Geometry, stopped=None, *, settling=()):
        if self._cancelled(stopped) or (0x1B not in settling and self.escape_pressed()):
            raise MacroError("입력 작업이 중지되었습니다.")
        x, y = self.cursor_position()
        if 0 <= x <= 3 and 0 <= y <= 3:
            raise MacroError("입력 작업이 중지되었습니다.")
        snapshot = self.snapshot(identity)
        _validate_snapshot(snapshot, identity, geometry)
        user, _ = self._apis()
        if user.GetForegroundWindow() != identity.hwnd:
            raise MacroError("선택한 브라우저의 전면 상태를 확인해 주세요.")
        if any(user.GetAsyncKeyState(key) & 0x8000 for key in _HELD_KEYS if key not in settling):
            raise MacroError("보조 키와 마우스 버튼을 놓은 뒤 다시 시도해 주세요.")
        return snapshot

    def _settle_keys(self, identity, geometry, packets, stopped=None) -> None:
        """Wait briefly for our keyups; never send input while keys are down.

        Only keys in the just-submitted batch get this grace interval. A key
        that remains held fails closed, and all other modifiers remain guarded.
        Focus/identity/geometry/cancellation are still checked during the wait.
        """
        keys = frozenset(vk for vk, _scan, _flags in (_packet_fields(event) for event in packets) if vk)
        if not keys:
            return
        user, _ = self._apis()
        deadline = self.monotonic() + _KEY_SETTLE_SECONDS
        stable = 0
        while True:
            self._guard(identity, geometry, stopped, settling=keys)
            if any(user.GetAsyncKeyState(key) & 0x8000 for key in keys):
                stable = 0
            else:
                stable += 1
                if stable >= 2:
                    return
            if self.monotonic() >= deadline:
                raise MacroError("입력 키가 해제되지 않았습니다. 키를 놓은 뒤 다시 시도해 주세요.")
            self.wait(0.005, stopped if stopped is not None and hasattr(stopped, "wait") else threading.Event())

    def _release_partial(self, packets, count: int | None) -> None:
        """Release only keys that this batch may have left down; never type."""
        pending: dict[tuple[int, int, int], None] = {}
        try:
            for index in range(len(packets) if count is None else count):
                vk, scan, flags = _packet_fields(packets[index])
                key = (vk, scan, flags & (_UNICODE | _EXTENDED))
                if flags & _KEYUP:
                    pending.pop(key, None)
                else:
                    pending[key] = None
            # On an exceptional return the accepted prefix is unknown. A keyup
            # for each potential keydown also covers an interrupted native call.
            if count is None:
                for event in packets:
                    vk, scan, flags = _packet_fields(event)
                    if not flags & _KEYUP:
                        pending[(vk, scan, flags & (_UNICODE | _EXTENDED))] = None
            user, _ = self._apis()
            for vk, scan, flags in reversed(pending):
                release = (_Input * 1)(_keyboard_packet(vk, scan, flags | _KEYUP))
                try:
                    user.SendInput(1, release, ctypes.sizeof(_Input))
                except Exception:
                    pass
                finally:
                    ctypes.memset(ctypes.addressof(release), 0, ctypes.sizeof(release))
        except Exception:
            pass
        finally:
            pending.clear()

    def _send_checked(self, identity, geometry, packets, stopped=None) -> None:
        try:
            self._guard(identity, geometry, stopped)
            user, _ = self._apis()
            try:
                sent = int(user.SendInput(len(packets), packets, ctypes.sizeof(_Input)))
            except Exception:
                self._release_partial(packets, None)
                raise MacroError("키 입력을 전달하지 못했습니다.") from None
            if sent != len(packets):
                self._release_partial(packets, sent if 0 <= sent < len(packets) else None)
                raise MacroError("키 입력을 모두 전달하지 못했습니다.")
            self._settle_keys(identity, geometry, packets, stopped)
            self._guard(identity, geometry, stopped)
        finally:
            ctypes.memset(ctypes.addressof(packets), 0, ctypes.sizeof(packets))

    def _combo(self, identity, geometry, keys: tuple[int, ...], stopped=None) -> None:
        events = []
        for key in keys:
            events.append(_keyboard_packet(key, flags=_EXTENDED if key in _EXTENDED_KEYS else 0))
        for key in reversed(keys):
            events.append(_keyboard_packet(key, flags=_KEYUP | (_EXTENDED if key in _EXTENDED_KEYS else 0)))
        packets = (_Input * len(events))(*events)
        try:
            self._send_checked(identity, geometry, packets, stopped)
        finally:
            for event in events:
                ctypes.memset(ctypes.byref(event), 0, ctypes.sizeof(event))

    def type_text(self, identity: WindowIdentity, geometry: Geometry, text: str, *,
                  clear: bool = True, stopped=None) -> None:
        encoded = bytearray()
        try:
            if (not isinstance(text, str) or len(text) > _MAX_TEXT or type(clear) is not bool
                    or any(ord(character) < 32 or ord(character) == 127 for character in text)):
                raise ValueError("invalid text input")
            encoded.extend(text.encode("utf-16-le"))
            self._guard(identity, geometry, stopped)
            if clear:
                self._combo(identity, geometry, (0x11, 0x41), stopped)  # Ctrl+A
                self._combo(identity, geometry, (0x08,), stopped)  # Backspace only for clear=True
            position = 0
            while position < len(encoded):
                end = min(position + _CHUNK_UNITS * 2, len(encoded))
                if end < len(encoded):
                    last = encoded[end - 2] | (encoded[end - 1] << 8)
                    if 0xD800 <= last <= 0xDBFF:
                        end -= 2  # Never split an astral character across guards/batches.
                packets = (_Input * (end - position))()
                for index, offset in enumerate(range(position, end, 2)):
                    unit = encoded[offset] | (encoded[offset + 1] << 8)
                    for shift, flags in ((0, _UNICODE), (1, _UNICODE | _KEYUP)):
                        event = _keyboard_packet(scan=unit, flags=flags)
                        packets[index * 2 + shift] = event
                        ctypes.memset(ctypes.byref(event), 0, ctypes.sizeof(event))
                self._send_checked(identity, geometry, packets, stopped)
                position = end
            self._guard(identity, geometry, stopped)
        except Exception:
            raise MacroError("선택한 브라우저에 텍스트를 입력하지 못했습니다. 창·키 상태와 중지 여부를 확인해 주세요.") from None
        finally:
            for index in range(len(encoded)):
                encoded[index] = 0
            encoded.clear()

    def press_key(self, identity: WindowIdentity, geometry: Geometry, key: str, *, stopped=None) -> None:
        try:
            normalized = key.strip().upper().replace(" ", "") if isinstance(key, str) else ""
            if normalized not in _KEYS:
                raise ValueError("unsupported key")
            self._combo(identity, geometry, _KEYS[normalized], stopped)
        except Exception:
            raise MacroError("선택한 브라우저에 허용된 키를 전달하지 못했습니다. 창·키 상태와 중지 여부를 확인해 주세요.") from None

    def navigate(self, identity: WindowIdentity, geometry: Geometry, url: str, *, stopped=None) -> None:
        try:
            if not isinstance(url, str) or not url or len(url) > _MAX_TEXT or any(character.isspace() or ord(character) < 32 for character in url):
                raise ValueError("invalid navigation target")
            parts = urlsplit(url)
            if parts.scheme.lower() not in ("https", "http") or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError("unsupported navigation target")
            self._combo(identity, geometry, (0x11, 0x4C), stopped)  # Browser address bar, Ctrl+L.
            self.type_text(identity, geometry, url, clear=False, stopped=stopped)
            self._combo(identity, geometry, (0x0D,), stopped)
        except Exception:
            raise MacroError("선택한 브라우저에서 주소를 열지 못했습니다. 창·키 상태와 중지 여부를 확인해 주세요.") from None

    @contextmanager
    def _automation(self):
        if self._uia_factory is not None:
            yield self._uia_factory()
            return
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        automation = timed = None
        try:
            module = comtypes.client.GetModule("UIAutomationCore.dll")
            automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
            # Optional bounded provider calls on systems with IUIAutomation2.
            if hasattr(module, "IUIAutomation2"):
                try:
                    timed = automation.QueryInterface(module.IUIAutomation2)
                    timed.ConnectionTimeout = 1000
                    timed.TransactionTimeout = 1000
                except Exception:
                    pass
            yield automation, module
        finally:
            timed = automation = None
            comtypes.CoUninitialize()

    @staticmethod
    def _top_rectangle(element, snapshot, checkpoint=None) -> bool:
        left, top, right, bottom = _rectangle(_uia_get(element, "CurrentBoundingRectangle", checkpoint))
        ceiling = snapshot.top + min(snapshot.geometry.height * 0.4, max(120, snapshot.geometry.dpi * 180 / 96))
        return (snapshot.left <= left < right <= snapshot.left + snapshot.geometry.width
                and snapshot.top <= top < bottom <= ceiling)

    def _address_candidate(self, element, ancestors, snapshot, identity, checkpoint=None) -> bool:
        def read(node, name):
            return _uia_get(node, name, checkpoint)
        if (int(read(element, "CurrentControlType")) != _UIA_EDIT or int(read(element, "CurrentProcessId")) != identity.pid
                or read(element, "CurrentIsOffscreen") or not read(element, "CurrentIsEnabled") or read(element, "CurrentIsPassword")):
            return False
        name = str(read(element, "CurrentName") or "").strip().casefold()
        automation_id = re.sub(r"[\s_\-]", "", str(read(element, "CurrentAutomationId") or "")).casefold()
        class_name = str(read(element, "CurrentClassName") or "").casefold()
        if name not in _ADDRESS_NAMES and automation_id not in _ADDRESS_IDS and class_name not in _ADDRESS_CLASSES:
            return False
        if not self._top_rectangle(element, snapshot, checkpoint):
            return False
        # Web document descendants are never inspected. In addition, require a
        # real browser-chrome toolbar ancestor inside the top client band.
        return any(int(read(parent, "CurrentControlType")) == _UIA_TOOLBAR and self._top_rectangle(parent, snapshot, checkpoint)
                   for parent in ancestors)

    @staticmethod
    def _normalize_url(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 8192:
            raise ValueError("missing browser address")
        value = value.strip()
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("invalid browser address")
        # Preserve explicit chrome/file/about/etc. schemes so the engine can
        # reject them. Omnibox providers may omit the HTTPS display prefix.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            return value
        return "https:" + value if value.startswith("//") else "https://" + value

    def current_url(self, identity: WindowIdentity) -> str:
        try:
            initial = self.snapshot(identity)
            self._guard(identity, initial.geometry)
            with self._automation() as (automation, module):
                root = walker = element = child = pattern = raw_pattern = None
                stack, candidates, children = [], [], []
                ancestors = ()
                try:
                    deadline = time.monotonic() + 2.0
                    next_guard = 0.0

                    def checkpoint():
                        nonlocal next_guard
                        now = time.monotonic()
                        if now >= deadline:
                            raise ValueError("browser chrome inspection limit")
                        if now >= next_guard:
                            self._guard(identity, initial.geometry)
                            next_guard = now + 0.05

                    checkpoint()
                    root = automation.ElementFromHandle(identity.hwnd)
                    checkpoint()
                    if (int(_uia_get(root, "CurrentNativeWindowHandle", checkpoint)) != identity.hwnd
                            or int(_uia_get(root, "CurrentProcessId", checkpoint)) != identity.pid):
                        raise ValueError("mismatched UIA root")
                    walker = _uia_get(automation, "ControlViewWalker", checkpoint)
                    stack = [(root, ())]
                    visited = 0
                    while stack:
                        checkpoint()
                        if visited >= 512:
                            raise ValueError("browser chrome inspection limit")
                        element, ancestors = stack.pop()
                        checkpoint()
                        visited += 1
                        kind = int(_uia_get(element, "CurrentControlType", checkpoint))
                        if kind == _UIA_DOCUMENT or len(ancestors) > 12:
                            continue
                        if self._address_candidate(element, ancestors, initial, identity, checkpoint):
                            candidates.append(element)
                        checkpoint()
                        child = walker.GetFirstChildElement(element)
                        children = []
                        while child:
                            checkpoint()
                            if len(children) >= 512:
                                raise ValueError("browser chrome inspection limit")
                            children.append(child)
                            child = walker.GetNextSiblingElement(child)
                            checkpoint()
                        stack.extend((child, (*ancestors, element)) for child in reversed(children))
                    checkpoint()
                    if len(candidates) != 1:
                        raise ValueError("ambiguous browser address bar")
                    raw_pattern = candidates[0].GetCurrentPattern(_UIA_VALUE_PATTERN)
                    checkpoint()
                    pattern = raw_pattern.QueryInterface(module.IUIAutomationValuePattern)
                    checkpoint()
                    value = self._normalize_url(_uia_get(pattern, "CurrentValue", checkpoint))
                    self._guard(identity, initial.geometry)
                finally:
                    # Drop COM element/pattern references before CoUninitialize.
                    root = walker = element = child = pattern = raw_pattern = automation = None
                    stack.clear()
                    candidates.clear()
                    children.clear()
                    ancestors = ()
                return value
        except Exception:
            raise MacroError("선택한 브라우저의 주소창을 확인하지 못했습니다. 주소창이 표시되는 정상 브라우저 창인지 확인해 주세요.") from None
