"""Small Win32 interop helpers used by the native CaptureWebsite GUI.

The main GUI module owns layout and application state. This module contains only the
low-level Windows APIs whose pointer/lifecycle details should not leak into that code.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


if sys.platform == "win32":
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t
    SUBCLASSPROC = ctypes.WINFUNCTYPE(
        LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        ULONG_PTR,
        ULONG_PTR,
    )

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    CLSID_FILE_OPEN_DIALOG = GUID(
        0xDC1C5A9C,
        0xE88A,
        0x4DDE,
        (ctypes.c_ubyte * 8)(0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7),
    )
    IID_IFILE_OPEN_DIALOG = GUID(
        0xD57C7288,
        0xD4AD,
        0x4768,
        (ctypes.c_ubyte * 8)(0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60),
    )
    IID_ISHELL_ITEM = GUID(
        0x43826D1E,
        0xE718,
        0x42EE,
        (ctypes.c_ubyte * 8)(0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE),
    )

    CLSCTX_INPROC_SERVER = 0x1
    FOS_PICKFOLDERS = 0x00000020
    FOS_FORCEFILESYSTEM = 0x00000040
    FOS_PATHMUSTEXIST = 0x00000800
    FOS_DONTADDTORECENT = 0x02000000
    SIGDN_FILESYSPATH = 0x80058000
    HRESULT_CANCELLED = 0x800704C7

    WM_SETFOCUS = 0x0007
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102
    WM_IME_COMPOSITION = 0x010F
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_IME_CHAR = 0x0286
    WM_CUT = 0x0300
    WM_PASTE = 0x0302
    WM_CLEAR = 0x0303
    WM_UNDO = 0x0304
    VK_BACK = 0x08
    VK_DELETE = 0x2E

    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    comctl32.SetWindowSubclass.restype = wintypes.BOOL
    comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ULONG_PTR, ULONG_PTR]
    comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
    comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ULONG_PTR]
    comctl32.DefSubclassProc.restype = LRESULT
    comctl32.DefSubclassProc.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.HideCaret.argtypes = [wintypes.HWND]
else:
    ole32 = shell32 = comctl32 = user32 = None
    SUBCLASSPROC = None


_READONLY_SUBCLASS_ID = 1


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("This Win32 helper is only available on Windows.")


def _com_method(interface: ctypes.c_void_p, index: int, restype: object, *argtypes: object):
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(address)


def choose_folder(owner_hwnd: int | None, initial_path: str, *, title: str) -> str | None:
    """Show the standard Explorer-style folder picker and return a filesystem path."""
    _require_windows()
    dialog = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(  # type: ignore[union-attr]
        ctypes.byref(CLSID_FILE_OPEN_DIALOG),
        None,
        CLSCTX_INPROC_SERVER,
        ctypes.byref(IID_IFILE_OPEN_DIALOG),
        ctypes.byref(dialog),
    )
    if hr < 0:
        raise OSError(f"IFileOpenDialog creation failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

    release_dialog = _com_method(dialog, 2, ctypes.c_ulong)
    try:
        options = ctypes.c_uint(0)
        get_options = _com_method(dialog, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))
        hr = get_options(dialog, ctypes.byref(options))
        if hr < 0:
            raise OSError(f"IFileOpenDialog.GetOptions failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

        options.value |= FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_DONTADDTORECENT
        set_options = _com_method(dialog, 9, ctypes.c_long, ctypes.c_uint)
        hr = set_options(dialog, options.value)
        if hr < 0:
            raise OSError(f"IFileOpenDialog.SetOptions failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

        set_title = _com_method(dialog, 17, ctypes.c_long, wintypes.LPCWSTR)
        hr = set_title(dialog, title)
        if hr < 0:
            raise OSError(f"IFileOpenDialog.SetTitle failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

        initial = Path(initial_path).expanduser()
        if initial.is_dir():
            initial_item = ctypes.c_void_p()
            initial_hr = shell32.SHCreateItemFromParsingName(  # type: ignore[union-attr]
                str(initial), None, ctypes.byref(IID_ISHELL_ITEM), ctypes.byref(initial_item)
            )
            if initial_hr >= 0 and initial_item.value:
                release_initial = _com_method(initial_item, 2, ctypes.c_ulong)
                try:
                    set_folder = _com_method(dialog, 12, ctypes.c_long, ctypes.c_void_p)
                    set_folder(dialog, initial_item)
                finally:
                    release_initial(initial_item)

        show = _com_method(dialog, 3, ctypes.c_long, wintypes.HWND)
        hr = show(dialog, owner_hwnd)
        if ctypes.c_uint32(hr).value == HRESULT_CANCELLED:
            return None
        if hr < 0:
            raise OSError(f"IFileOpenDialog.Show failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

        shell_item = ctypes.c_void_p()
        get_result = _com_method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        hr = get_result(dialog, ctypes.byref(shell_item))
        if hr < 0 or not shell_item.value:
            raise OSError(f"IFileOpenDialog.GetResult failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X}).")

        release_item = _com_method(shell_item, 2, ctypes.c_ulong)
        try:
            raw_path = ctypes.c_void_p()
            get_display_name = _com_method(
                shell_item, 5, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
            )
            hr = get_display_name(shell_item, SIGDN_FILESYSPATH, ctypes.byref(raw_path))
            if hr < 0 or not raw_path.value:
                raise OSError(
                    f"IShellItem.GetDisplayName failed (HRESULT 0x{ctypes.c_uint32(hr).value:08X})."
                )
            try:
                return ctypes.wstring_at(raw_path.value)
            finally:
                ole32.CoTaskMemFree(raw_path)  # type: ignore[union-attr]
        finally:
            release_item(shell_item)
    finally:
        release_dialog(dialog)


def _readonly_edit_proc(
    hwnd: int,
    message: int,
    wparam: int,
    lparam: int,
    _subclass_id: int,
    _ref_data: int,
) -> int:
    if message in {WM_CHAR, WM_IME_CHAR, WM_IME_COMPOSITION, WM_PASTE, WM_CUT, WM_CLEAR, WM_UNDO}:
        return 0
    if message == WM_KEYDOWN and int(wparam) in {VK_BACK, VK_DELETE}:
        return 0
    result = comctl32.DefSubclassProc(hwnd, message, wparam, lparam)  # type: ignore[union-attr]
    if message in {WM_SETFOCUS, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_KEYDOWN, WM_KEYUP}:
        user32.HideCaret(hwnd)  # type: ignore[union-attr]
    return result


if sys.platform == "win32":
    _READONLY_EDIT_PROC = SUBCLASSPROC(_readonly_edit_proc)
else:
    _READONLY_EDIT_PROC = None


def install_readonly_edit_guard(hwnd: int) -> None:
    """Prevent user edits while preserving selection, scrolling and copying."""
    _require_windows()
    if not comctl32.SetWindowSubclass(  # type: ignore[union-attr]
        hwnd, _READONLY_EDIT_PROC, _READONLY_SUBCLASS_ID, 0
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def remove_readonly_edit_guard(hwnd: int) -> None:
    """Release the subclass installed by :func:`install_readonly_edit_guard`."""
    if sys.platform != "win32" or not _READONLY_EDIT_PROC:
        return
    comctl32.RemoveWindowSubclass(  # type: ignore[union-attr]
        hwnd, _READONLY_EDIT_PROC, _READONLY_SUBCLASS_ID
    )
