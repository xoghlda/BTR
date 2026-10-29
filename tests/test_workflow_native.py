"""Mock-only native adapter tests: never load UIA or send real Windows input."""
from __future__ import annotations

import ctypes
from dataclasses import replace
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trip_capture.browser_macro import Geometry, MacroError, WindowIdentity, WindowSnapshot
from trip_capture.workflow_native import WorkflowNativeAdapter, _packet_fields


IDENTITY = WindowIdentity(101, 42, 1234, r"C:\Program Files\Browser\msedge.exe")
GEOMETRY = Geometry(800, 600, 96)


class FakeUser:
    def __init__(self):
        self.foreground = IDENTITY.hwnd
        self.physical = set()
        self.injected = set()
        self.calls = []
        self.buffers = []
        self.outcomes = []
        self.after_send = None

    def GetForegroundWindow(self):
        return self.foreground

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.physical or key in self.injected else 0

    def SendInput(self, count, packets, _size):
        fields = [_packet_fields(packets[index]) for index in range(count)]
        self.calls.append(fields)
        self.buffers.append(packets)
        outcome = self.outcomes.pop(0) if self.outcomes else count
        if isinstance(outcome, Exception):
            raise outcome
        for vk, _scan, flags in fields[:outcome]:
            if vk:
                self.injected.discard(vk) if flags & 2 else self.injected.add(vk)
        if self.after_send:
            self.after_send()
        return outcome


class FakeNode:
    def __init__(self, *, kind=50033, name="", automation_id="", rect=(100, 100, 900, 700),
                 pid=42, hwnd=0, value="", password=False, offscreen=False, enabled=True):
        self.CurrentControlType = kind
        self.CurrentName = name
        self.CurrentAutomationId = automation_id
        self.CurrentClassName = ""
        self.CurrentBoundingRectangle = rect
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = hwnd
        self.CurrentIsPassword = password
        self.CurrentIsOffscreen = offscreen
        self.CurrentIsEnabled = enabled
        self.children = []
        self.parent = None
        self.value = value
        self.value_reads = 0

    def add(self, *children):
        for child in children:
            child.parent = self
            self.children.append(child)
        return self

    def GetCurrentPattern(self, identifier):
        if identifier != 10002:
            raise AssertionError("unexpected pattern")
        self.value_reads += 1
        node = self

        class Pattern:
            def QueryInterface(self, _interface):
                return self

            @property
            def CurrentValue(self):
                return node.value

        return Pattern()


class FakeWalker:
    def __init__(self):
        self.children_requested = []

    def GetFirstChildElement(self, node):
        self.children_requested.append(node)
        return node.children[0] if node.children else None

    def GetNextSiblingElement(self, node):
        siblings = node.parent.children
        index = siblings.index(node) + 1
        return siblings[index] if index < len(siblings) else None


class FakeAdapter(WorkflowNativeAdapter):
    def __init__(self, root=None):
        self.user = FakeUser()
        self.state = WindowSnapshot(IDENTITY, GEOMETRY, 100, 100)
        self.escaped = False
        self.pointer = (500, 500)
        self.snapshots = 0
        self.walker = FakeWalker()
        self.root = root
        automation = SimpleNamespace(ElementFromHandle=lambda _hwnd: self.root, ControlViewWalker=self.walker)
        module = SimpleNamespace(IUIAutomationValuePattern=object())
        super().__init__(uia_factory=lambda: (automation, module))

    def _apis(self):
        return self.user, None

    def snapshot(self, _identity):
        self.snapshots += 1
        return self.state

    def escape_pressed(self):
        return self.escaped

    def cursor_position(self):
        return self.pointer


def browser_tree(value="www.hipass.co.kr/receipt"):
    root = FakeNode(hwnd=IDENTITY.hwnd)
    toolbar = FakeNode(kind=50021, rect=(100, 135, 900, 200))
    address = FakeNode(kind=50004, name="Address and search bar", rect=(180, 145, 850, 180), value=value)
    root.add(toolbar.add(address))
    return root, toolbar, address


class NativeKeyboardTests(unittest.TestCase):
    def asynchronous_adapter(self):
        adapter = FakeAdapter()
        clock = [0.0]
        pending = {}
        original_state = adapter.user.GetAsyncKeyState
        adapter.monotonic = lambda: clock[0]
        adapter.wait = lambda seconds, _stopped: clock.__setitem__(0, clock[0] + seconds)
        adapter.user.GetAsyncKeyState = lambda key: 0x8000 if clock[0] < pending.get(key, -1) else original_state(key)
        adapter.escape_pressed = lambda: bool(adapter.user.GetAsyncKeyState(0x1B) & 0x8000)
        def delayed_keyup():
            for vk, _scan, _flags in adapter.user.calls[-1]:
                if vk:
                    pending[vk] = clock[0] + 0.02
        adapter.user.after_send = delayed_keyup
        return adapter, clock

    def test_own_delayed_ctrl_and_esc_keyups_settle_before_post_guard(self):
        for key in ("CTRL+A", "ESC"):
            with self.subTest(key=key):
                adapter, clock = self.asynchronous_adapter()
                adapter.press_key(IDENTITY, GEOMETRY, key)
                self.assertGreaterEqual(clock[0], 0.025)
                self.assertLess(clock[0], 0.15)
                self.assertEqual(len(adapter.user.calls), 1)

    def test_user_modifier_still_held_after_injected_keyup_fails_closed(self):
        adapter, clock = self.asynchronous_adapter()
        delayed = adapter.user.after_send
        def hold_ctrl():
            delayed()
            adapter.user.physical.add(0x11)
        adapter.user.after_send = hold_ctrl
        with self.assertRaises(MacroError): adapter.press_key(IDENTITY, GEOMETRY, "CTRL+A")
        self.assertGreaterEqual(clock[0], 0.15)
        self.assertEqual(len(adapter.user.calls), 1)

    def test_focus_and_cancel_remain_guarded_while_keyup_is_settling(self):
        for cause in ("focus", "cancel"):
            with self.subTest(cause=cause):
                adapter, clock = self.asynchronous_adapter()
                stopped = threading.Event()
                def wait(seconds, _stopped):
                    clock[0] += seconds
                    if cause == "focus": adapter.user.foreground = 999
                    else: stopped.set()
                adapter.wait = wait
                with self.assertRaises(MacroError): adapter.press_key(IDENTITY, GEOMETRY, "CTRL+A", stopped=stopped)
                self.assertLess(clock[0], 0.02)
                self.assertEqual(len(adapter.user.calls), 1)

    def test_unicode_uses_utf16_down_up_and_zeroes_native_buffers(self):
        adapter = FakeAdapter()
        text = "한글 😀 test"
        adapter.type_text(IDENTITY, GEOMETRY, text, clear=False)
        down_units = [scan for call in adapter.user.calls for vk, scan, flags in call if flags == 4]
        encoded = b"".join(unit.to_bytes(2, "little") for unit in down_units)
        self.assertEqual(encoded.decode("utf-16-le"), text)
        self.assertTrue(all(vk == 0 and flags in (4, 6) for call in adapter.user.calls for vk, _scan, flags in call))
        self.assertGreaterEqual(adapter.snapshots, 4)
        self.assertTrue(all(not any(bytes(buffer)) for buffer in adapter.user.buffers))

    def test_clear_uses_ctrl_a_then_backspace_before_text(self):
        adapter = FakeAdapter()
        adapter.type_text(IDENTITY, GEOMETRY, "fixture")
        self.assertEqual([vk for vk, _scan, _flags in adapter.user.calls[0]], [0x11, 0x41, 0x41, 0x11])
        self.assertEqual(adapter.user.calls[1], [(8, 0, 0), (8, 0, 2)])
        self.assertFalse(adapter.user.injected)

    def test_cancel_between_chunks_stops_without_splitting_surrogate_pairs(self):
        adapter = FakeAdapter()
        stopped = threading.Event()
        adapter.user.after_send = stopped.set
        with self.assertRaises(MacroError):
            adapter.type_text(IDENTITY, GEOMETRY, "a" * 15 + "😀" + "z" * 30, clear=False, stopped=stopped)
        self.assertEqual(len(adapter.user.calls), 1)
        units = [scan for _vk, scan, flags in adapter.user.calls[0] if flags == 4]
        self.assertEqual(len(units), 15)
        self.assertTrue(all(not any(bytes(buffer)) for buffer in adapter.user.buffers))

    def test_identity_geometry_focus_modifiers_and_cancel_block_all_input(self):
        for cause in ("identity", "geometry", "snapshot-focus", "foreground", "modifier", "cancel", "escape", "corner"):
            with self.subTest(cause=cause):
                adapter = FakeAdapter()
                stopped = threading.Event()
                if cause == "identity": adapter.state = replace(adapter.state, identity=replace(IDENTITY, pid=999))
                if cause == "geometry": adapter.state = replace(adapter.state, geometry=Geometry(801, 600, 96))
                if cause == "snapshot-focus": adapter.state = replace(adapter.state, foreground=False)
                if cause == "foreground": adapter.user.foreground = 999
                if cause == "modifier": adapter.user.physical.add(0x11)
                if cause == "cancel": stopped.set()
                if cause == "escape": adapter.escaped = True
                if cause == "corner": adapter.pointer = (0, 0)
                with self.assertRaises(MacroError):
                    adapter.type_text(IDENTITY, GEOMETRY, "PRIVATE fixture", stopped=stopped)
                self.assertEqual(adapter.user.calls, [])

    def test_focus_change_after_first_chunk_prevents_remaining_text(self):
        adapter = FakeAdapter()
        adapter.user.after_send = lambda: setattr(adapter.user, "foreground", 999)
        with self.assertRaises(MacroError):
            adapter.type_text(IDENTITY, GEOMETRY, "a" * 50, clear=False)
        self.assertEqual(len(adapter.user.calls), 1)
        self.assertEqual(len(adapter.user.calls[0]), 32)

    def test_partial_chord_releases_only_injected_pressed_keys(self):
        adapter = FakeAdapter()
        adapter.user.outcomes = [2]
        with self.assertRaises(MacroError):
            adapter.press_key(IDENTITY, GEOMETRY, "CTRL+P")
        self.assertEqual(adapter.user.calls[1:], [[(0x50, 0, 2)], [(0x11, 0, 2)]])
        self.assertFalse(adapter.user.injected)
        self.assertTrue(all(not any(bytes(buffer)) for buffer in adapter.user.buffers))

    def test_partial_unicode_releases_scan_and_never_replays_character(self):
        adapter = FakeAdapter()
        adapter.user.outcomes = [1]
        with self.assertRaises(MacroError):
            adapter.type_text(IDENTITY, GEOMETRY, "한", clear=False)
        self.assertEqual(adapter.user.calls[1], [(0, ord("한"), 6)])

    def test_only_allowed_keys_and_extended_dropdown_keys_are_sent(self):
        for key in ("ENTER", "TAB", "SHIFT+TAB", "CTRL+A", "CTRL+P", "ESC", "SPACE", "DOWN", "UP", "HOME", "END"):
            with self.subTest(key=key):
                adapter = FakeAdapter()
                adapter.press_key(IDENTITY, GEOMETRY, key)
                if key in ("DOWN", "UP", "HOME", "END"):
                    self.assertEqual([flags for _vk, _scan, flags in adapter.user.calls[0]], [1, 3])
                self.assertFalse(adapter.user.injected)
        for key in ("ALT+F4", "WIN+R", "CTRL+L", "DELETE", "PRIVATE_TOKEN"):
            adapter = FakeAdapter()
            with self.assertRaises(MacroError):
                adapter.press_key(IDENTITY, GEOMETRY, key)
            self.assertEqual(adapter.user.calls, [])

    def test_navigate_is_ctrl_l_unicode_enter_in_selected_window(self):
        adapter = FakeAdapter()
        adapter.navigate(IDENTITY, GEOMETRY, "https://www.hipass.co.kr/")
        self.assertEqual([vk for vk, _scan, _flags in adapter.user.calls[0]], [0x11, 0x4C, 0x4C, 0x11])
        self.assertEqual(adapter.user.calls[-1], [(13, 0, 0), (13, 0, 2)])
        self.assertFalse(adapter.user.injected)
        for url in ("file:///private", "javascript:alert(1)", "https://name:secret@example.com/", "https://example.com/\n"):
            adapter = FakeAdapter()
            with self.assertRaises(MacroError): adapter.navigate(IDENTITY, GEOMETRY, url)
            self.assertEqual(adapter.user.calls, [])

    def test_errors_do_not_expose_input_or_native_exception_values(self):
        adapter = FakeAdapter()
        adapter.user.outcomes = [RuntimeError("PRIVATE_SECRET https://private.example/?token=PRIVATE_TOKEN")]
        try:
            adapter.type_text(IDENTITY, GEOMETRY, "PRIVATE_PASSWORD", clear=False)
        except MacroError as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn("PRIVATE_SECRET", rendered)
            self.assertNotIn("PRIVATE_TOKEN", rendered)
            self.assertNotIn("PRIVATE_PASSWORD", str(error))
        else:
            self.fail("native failure must fail closed")


class NativeAddressBarTests(unittest.TestCase):
    def test_slow_candidate_properties_check_deadline_between_rpc_calls(self):
        root, toolbar, original = browser_tree()
        clock = [0.0]
        reads = [0]
        class SlowAddress(FakeNode):
            def __getattribute__(self, name):
                if name.startswith("Current"):
                    reads[0] += 1
                    clock[0] += 0.6
                return super().__getattribute__(name)
        address = SlowAddress(kind=50004, name="Address and search bar", rect=(180,145,850,180), value="https://www.hipass.co.kr/")
        toolbar.children.clear()
        toolbar.add(address)
        adapter = FakeAdapter(root)
        with patch("trip_capture.workflow_native.time", SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertRaises(MacroError): adapter.current_url(IDENTITY)
        self.assertLessEqual(reads[0], 4)
        self.assertEqual(address.value_reads, 0)
        self.assertEqual(original.value_reads, 0)
        self.assertEqual(adapter.user.calls, [])

    def test_slow_sibling_provider_checks_deadline_inside_child_loop(self):
        root, _toolbar, address = browser_tree()
        root.add(*(FakeNode() for _index in range(30)))
        adapter = FakeAdapter(root)
        clock = [0.0]
        calls = [0]
        original_next = adapter.walker.GetNextSiblingElement
        def slow_next(node):
            calls[0] += 1
            clock[0] += 0.8
            return original_next(node)
        adapter.walker.GetNextSiblingElement = slow_next
        with patch("trip_capture.workflow_native.time", SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertRaises(MacroError): adapter.current_url(IDENTITY)
        self.assertLessEqual(calls[0], 3)
        self.assertEqual(address.value_reads, 0)
        self.assertEqual(adapter.user.calls, [])

    def test_focus_change_inside_sibling_walk_is_checked_before_value_read(self):
        root, _toolbar, address = browser_tree()
        adapter = FakeAdapter(root)
        clock = [0.0]
        original_next = adapter.walker.GetNextSiblingElement
        def changed_next(node):
            clock[0] += 0.1
            adapter.user.foreground = 999
            return original_next(node)
        adapter.walker.GetNextSiblingElement = changed_next
        with patch("trip_capture.workflow_native.time", SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertRaises(MacroError): adapter.current_url(IDENTITY)
        self.assertEqual(address.value_reads, 0)

    def test_real_chrome_toolbar_address_only_and_scheme_normalization(self):
        for raw, expected in (("www.hipass.co.kr/receipt", "https://www.hipass.co.kr/receipt"),
                              ("https://www.korail.com/ticket", "https://www.korail.com/ticket"),
                              ("chrome://settings", "chrome://settings"), ("file:///private", "file:///private")):
            with self.subTest(raw=raw):
                root, _toolbar, address = browser_tree(raw)
                adapter = FakeAdapter(root)
                self.assertEqual(adapter.current_url(IDENTITY), expected)
                self.assertEqual(address.value_reads, 1)
                self.assertEqual(adapter.user.calls, [])

    def test_document_edit_spoof_is_never_traversed_or_read(self):
        root, _toolbar, address = browser_tree()
        document = FakeNode(kind=50030)
        spoof_toolbar = FakeNode(kind=50021, rect=(100, 135, 900, 200))
        spoof = FakeNode(kind=50004, name="Address and search bar", rect=(180, 145, 850, 180), value="https://untrusted.example/")
        root.add(document.add(spoof_toolbar.add(spoof)))
        adapter = FakeAdapter(root)
        self.assertEqual(adapter.current_url(IDENTITY), "https://www.hipass.co.kr/receipt")
        self.assertEqual(spoof.value_reads, 0)
        self.assertNotIn(document, adapter.walker.children_requested)

    def test_name_id_toolbar_rect_pid_and_password_checks_fail_closed(self):
        for cause in ("name", "toolbar", "rect", "pid", "password", "offscreen", "disabled", "duplicate", "root"):
            with self.subTest(cause=cause):
                root, toolbar, address = browser_tree()
                if cause == "name": address.CurrentName = "Page login field"
                if cause == "toolbar": toolbar.CurrentControlType = 50033
                if cause == "rect": address.CurrentBoundingRectangle = (180, 400, 850, 440)
                if cause == "pid": address.CurrentProcessId = 999
                if cause == "password": address.CurrentIsPassword = True
                if cause == "offscreen": address.CurrentIsOffscreen = True
                if cause == "disabled": address.CurrentIsEnabled = False
                if cause == "duplicate": toolbar.add(FakeNode(kind=50004, automation_id="addressEditBox", rect=(180,145,850,180)))
                if cause == "root": root.CurrentNativeWindowHandle = 999
                adapter = FakeAdapter(root)
                with self.assertRaises(MacroError): adapter.current_url(IDENTITY)
                self.assertEqual(address.value_reads, 0)
                self.assertEqual(adapter.user.calls, [])

    def test_missing_uia_and_private_provider_error_are_generic(self):
        adapter = FakeAdapter()
        def unavailable():
            raise RuntimeError("PRIVATE_URL https://secret.example/?password=PRIVATE_PASSWORD")
        adapter._uia_factory = unavailable
        try:
            adapter.current_url(IDENTITY)
        except MacroError as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn("PRIVATE_URL", rendered)
            self.assertNotIn("PRIVATE_PASSWORD", rendered)
        else:
            self.fail("unavailable UIA must fail closed")


if __name__ == "__main__":
    unittest.main()
