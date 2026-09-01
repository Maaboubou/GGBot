"""Windows 剪贴板辅助。"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from pathlib import Path

CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002


def is_windows() -> bool:
    return os.name == "nt"


def _win32_modules():
    """返回声明了 64 位安全原型的 kernel32/user32。

    不设置 argtypes/restype 时，ctypes 会把句柄按 32 位整数转换；
    64 位系统上 GlobalAlloc 返回的句柄一旦超过 32 位就会溢出。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    return kernel32, user32


def set_text(text: str) -> None:
    """设置文本到剪贴板。"""
    if not is_windows():
        raise RuntimeError("剪贴板功能只能在 Windows 上使用")
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception:
        pass
    _set_cf_unicodetext(text)


def get_text() -> str:
    if not is_windows():
        raise RuntimeError("剪贴板功能只能在 Windows 上使用")
    try:
        import pyperclip

        return pyperclip.paste()
    except Exception:
        return ""


def clear() -> None:
    """清空剪贴板。"""
    if not is_windows():
        return
    try:
        _, user32 = _win32_modules()
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()
    except Exception:
        pass


def read_files() -> list[str]:
    """读取剪贴板 CF_HDROP 中的文件路径列表。"""
    if not is_windows():
        raise RuntimeError("剪贴板文件功能只能在 Windows 上使用")
    kernel32, user32 = _win32_modules()
    if not user32.OpenClipboard(None):
        return []
    try:
        hglobal = user32.GetClipboardData(CF_HDROP)
        if not hglobal:
            return []
        size = kernel32.GlobalSize(hglobal)
        if size <= 20:
            return []
        ptr = kernel32.GlobalLock(hglobal)
        if not ptr:
            return []
        try:
            data = ctypes.string_at(ptr, size)
            offset = struct.unpack_from("I", data, 0)[0]
            if offset >= len(data):
                return []
            raw = data[offset : len(data) - 2]
            text = raw.decode("utf-16-le", errors="ignore")
            return [part for part in text.split("\x00") if part.strip()]
        finally:
            kernel32.GlobalUnlock(hglobal)
    finally:
        user32.CloseClipboard()


def set_files(paths) -> None:
    """把文件路径列表放入剪贴板 CF_HDROP 格式。"""
    if not is_windows():
        raise RuntimeError("剪贴板文件功能只能在 Windows 上使用")
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not paths:
        raise ValueError("文件路径列表不能为空")
    normalized = [str(Path(path).resolve()) for path in paths]

    # DROPFILES: DWORD pFiles + POINT pt + BOOL fNC + BOOL fWide = 20 字节。
    wide_paths = "\x00".join(normalized) + "\x00\x00"
    payload = wide_paths.encode("utf-16-le")
    buffer_size = 20 + len(payload)
    buf = ctypes.create_string_buffer(buffer_size)
    struct.pack_into("IiiII", buf, 0, 20, 0, 0, 0, 1)
    ctypes.memmove(ctypes.addressof(buf) + 20, payload, len(payload))

    kernel32, user32 = _win32_modules()
    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, buffer_size)
    if not hglobal:
        raise OSError("GlobalAlloc 失败")
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        kernel32.GlobalFree(hglobal)
        raise OSError("GlobalLock 失败")
    try:
        ctypes.memmove(ptr, buf, buffer_size)
    finally:
        kernel32.GlobalUnlock(hglobal)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(hglobal)
        raise OSError("OpenClipboard 失败")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_HDROP, hglobal):
            kernel32.GlobalFree(hglobal)
            raise OSError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


def _set_cf_unicodetext(text: str) -> None:
    """ctypes 后备文本剪贴板实现。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    kernel32, user32 = _win32_modules()

    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not hglobal:
        raise OSError("GlobalAlloc 失败")
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        kernel32.GlobalFree(hglobal)
        raise OSError("GlobalLock 失败")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(hglobal)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(hglobal)
        raise OSError("OpenClipboard 失败")
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, hglobal)
    finally:
        user32.CloseClipboard()
