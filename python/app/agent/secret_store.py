"""Store the optional DeepSeek key in this Windows user's Credential Manager.

The credential is outside the repository. The UI reads it at startup; the
command-line helper accepts the value from a hidden terminal prompt or stdin.
"""

import ctypes
import getpass
import os
import sys
from ctypes import wintypes


TARGET_NAME = "ZHJammerV4/DeepSeekAPI"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if os.name != "nt":
        raise OSError("Windows Credential Manager is available on Windows only")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    )
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = (ctypes.POINTER(_Credential), wintypes.DWORD)
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = (ctypes.c_void_p,)
    api.CredFree.restype = None
    return api


def read_api_key() -> str:
    """Return this user's stored key, or an empty string when none is saved."""
    if os.name != "nt":
        return ""
    api = _advapi32()
    ptr = ctypes.POINTER(_Credential)()
    if not api.CredReadW(TARGET_NAME, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return ""
        raise ctypes.WinError(error)
    try:
        credential = ptr.contents
        raw = ctypes.string_at(credential.CredentialBlob,
                                credential.CredentialBlobSize)
        return raw.decode("utf-8")
    finally:
        api.CredFree(ptr)


def save_api_key(key: str) -> None:
    """Save a key for the current Windows user without writing a project file."""
    key = key.strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("A nonempty single-line API key is required")
    raw = key.encode("utf-8")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = _Credential()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = TARGET_NAME
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = blob
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = getpass.getuser()
    api = _advapi32()
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_api_key() -> None:
    """Remove this user's saved key (an absent key is already removed)."""
    api = _advapi32()
    if not api.CredDeleteW(TARGET_NAME, _CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != _ERROR_NOT_FOUND:
            raise ctypes.WinError(error)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("set", "status", "delete"):
        raise SystemExit("Usage: python -m agent.secret_store set|status|delete")
    command = sys.argv[1]
    if command == "set":
        key = (getpass.getpass("DeepSeek API key: ") if sys.stdin.isatty()
               else sys.stdin.readline())
        save_api_key(key)
        print("API key saved for the current Windows user.")
    elif command == "status":
        print("API key configured." if read_api_key() else "API key not configured.")
    else:
        delete_api_key()
        print("API key removed.")


if __name__ == "__main__":
    main()
