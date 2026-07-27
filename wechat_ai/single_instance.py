from __future__ import annotations

import ctypes
import sys
from typing import Any


ERROR_ALREADY_EXISTS = 183
MUTEX_PREFIX = "Local\\tqy-wechat-auto-reply-"
WECHAT_DESKTOP_MUTEX_NAME = f"{MUTEX_PREFIX}wechat-desktop-v1"


class SingleInstanceAlreadyRunningError(RuntimeError):
    """Raised when another bot owns the same instance mutex."""


class SingleInstanceUnsupportedError(RuntimeError):
    """Raised when named Windows mutexes are unavailable."""


def build_mutex_name(config: object, env_path: str) -> str:
    # One Windows login session controls one WeChat desktop client. Configuration
    # values must not be able to bypass the process lock for that shared UI.
    del config, env_path
    return WECHAT_DESKTOP_MUTEX_NAME


def _load_kernel32() -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise SingleInstanceUnsupportedError(
            "Single-instance locking requires Windows named mutex support."
        )

    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


class SingleInstanceLock:
    def __init__(
        self,
        mutex_name: str,
        *,
        platform_name: str | None = None,
        kernel32: Any | None = None,
    ) -> None:
        self.mutex_name = mutex_name
        self._platform_name = platform_name or sys.platform
        self._kernel32 = kernel32
        self._handle: int | None = None

    @classmethod
    def from_config(
        cls,
        config: object,
        env_path: str,
    ) -> "SingleInstanceLock":
        return cls(build_mutex_name(config, env_path))

    def acquire(self) -> "SingleInstanceLock":
        if self._handle is not None:
            return self
        if self._platform_name != "win32":
            raise SingleInstanceUnsupportedError(
                f"Single-instance locking requires Windows named mutex support; "
                f"platform {self._platform_name!r} is unsupported."
            )

        kernel32 = self._kernel32 or _load_kernel32()
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self.mutex_name)
        error_code = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error_code)
        if error_code == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise SingleInstanceAlreadyRunningError(
                "Another WeChat automation instance is already running in this Windows session."
            )

        self._kernel32 = kernel32
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return

        handle = self._handle
        kernel32 = self._kernel32
        self._handle = None
        if kernel32 is None:
            return

        if not kernel32.ReleaseMutex(handle):
            error_code = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error_code)
        kernel32.CloseHandle(handle)

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
