from __future__ import annotations

import ctypes
import os


_SERVICE = "GithubManager-PySide6"
_ACCOUNT = "github-access-token"


class CredentialStoreError(RuntimeError):
    pass


if os.name == "nt":
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CredentialAttributeW(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    class _CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FileTime),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(_CredentialAttributeW)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


class CredentialStore:
    @staticmethod
    def load() -> str | None:
        if os.name == "nt":
            pointer = ctypes.POINTER(_CredentialW)()
            if not _advapi32.CredReadW(_ACCOUNT, 1, 0, ctypes.byref(pointer)):
                error = ctypes.get_last_error()
                if error == 1168:
                    return None
                raise CredentialStoreError(f"Windows Credential Manager read failed ({error}).")
            try:
                credential = pointer.contents
                value = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
                return value.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                raise CredentialStoreError("The saved GitHub credential is invalid.") from None
            finally:
                _advapi32.CredFree(pointer)

        try:
            import keyring

            return keyring.get_password(_SERVICE, _ACCOUNT)
        except Exception as error:
            raise CredentialStoreError(f"Secure credential storage is unavailable: {error}") from None

    @staticmethod
    def save(token: str) -> None:
        if os.name == "nt":
            encoded = token.encode("utf-8")
            buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
            credential = _CredentialW()
            credential.Type = 1
            credential.TargetName = _ACCOUNT
            credential.CredentialBlobSize = len(encoded)
            credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = 2
            credential.UserName = "Github Manager"
            if not _advapi32.CredWriteW(ctypes.byref(credential), 0):
                error = ctypes.get_last_error()
                raise CredentialStoreError(f"Windows Credential Manager write failed ({error}).")
            return

        try:
            import keyring

            keyring.set_password(_SERVICE, _ACCOUNT, token)
        except Exception as error:
            raise CredentialStoreError(f"Secure credential storage is unavailable: {error}") from None

    @staticmethod
    def delete() -> None:
        if os.name == "nt":
            if not _advapi32.CredDeleteW(_ACCOUNT, 1, 0):
                error = ctypes.get_last_error()
                if error != 1168:
                    raise CredentialStoreError(f"Windows Credential Manager delete failed ({error}).")
            return

        try:
            import keyring

            try:
                keyring.delete_password(_SERVICE, _ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
        except Exception as error:
            raise CredentialStoreError(f"Secure credential storage is unavailable: {error}") from None
