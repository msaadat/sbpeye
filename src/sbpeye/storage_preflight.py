"""Storage exclusivity checks that run before importing the application."""

import os
from pathlib import Path


def require_exclusive_store(root: Path):
    root = root.resolve()
    if not root.exists():
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []
        try:
            for path in ([root] if root.is_file() else root.rglob("*")):
                if not path.is_file():
                    continue
                handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise ValueError(f"Cannot establish exclusive vector-store access ({path.name}); stop all corpus writers first.")
                handles.append(handle)
        finally:
            for handle in handles:
                kernel.CloseHandle(handle)
        return
    try:
        import psutil
    except ImportError as exc:
        raise ValueError("Cannot verify vector-store exclusivity: psutil is unavailable") from exc
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        try:
            for handle in process.open_files():
                if Path(handle.path).resolve() == root or Path(handle.path).resolve().is_relative_to(root):
                    raise ValueError(f"Process {process.pid} holds the vector store; stop it first")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ValueError(f"Cannot inspect process {process.pid}; exclusivity is unknown") from exc
