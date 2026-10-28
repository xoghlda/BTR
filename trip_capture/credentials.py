"""Use the current Windows user's Credential Manager, without a plaintext file."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_PREFIX = "TripEvidenceCapture/"
_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("로그인 정보 저장은 Windows에서 사용할 수 있습니다.")
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    @staticmethod
    def _target(account: str) -> str:
        if not account.strip() or len(account) > 500:
            raise ValueError("하이패스 아이디를 확인해 주세요.")
        return _PREFIX + account

    def set(self, account: str, password: str) -> None:
        target = self._target(account)
        encoded = password.encode("utf-16-le")
        if not encoded or len(encoded) > 2560:
            raise ValueError("비밀번호를 입력해 주세요. 최대 1,280자까지 저장할 수 있습니다.")
        buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _Credential()
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = "출장 증빙 캡처 - 하이패스 로그인"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, current user only
        credential.UserName = account
        try:
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            ctypes.memset(buffer, 0, len(encoded))

    def get(self, account: str) -> str | None:
        target = self._target(account)
        pointer = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            item = pointer.contents
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
        finally:
            self._api.CredFree(pointer)

    def delete(self, account: str) -> None:
        if not self._api.CredDeleteW(self._target(account), 1, 0):
            error = ctypes.get_last_error()
            if error != _NOT_FOUND:
                raise ctypes.WinError(error)
