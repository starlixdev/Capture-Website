"""Native Win32 GUI for CaptureWebsite, backed by the shared CaptureEngine."""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from sitecapture.capture.native import open_persistent_browser_session
from sitecapture.config import (
    BrowserSessionMode,
    CaptureConfig,
    load_browser_session_preference,
    load_output_preference,
    save_browser_session_preference,
    save_output_preference,
)
from sitecapture.core import CaptureEngine, CaptureResult
from sitecapture.gui.win32 import choose_folder, install_readonly_edit_guard, remove_readonly_edit_guard
from sitecapture.version import __version__

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
else:
    user32 = kernel32 = gdi32 = dwmapi = uxtheme = ole32 = comctl32 = None

WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
WM_DRAWITEM = 0x002B
WM_COMMAND = 0x0111
WM_KEYDOWN = 0x0100
WM_CHAR = 0x0102
WM_PASTE = 0x0302
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLOREDIT = 0x0133
WM_SETFONT = 0x0030
WM_APP_EVENT = 0x8001
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CLIPCHILDREN = 0x02000000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
ES_AUTOHSCROLL = 0x0080
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800
BS_OWNERDRAW = 0x000B
CBS_DROPDOWNLIST = 0x0003
CBS_HASSTRINGS = 0x0200
SS_LEFT = 0x0000
SS_RIGHT = 0x0002
SS_NOPREFIX = 0x0080
CW_USEDEFAULT = 0x80000000
SW_HIDE = 0
SW_SHOW = 5
IDC_ARROW = 32512
IDI_APPLICATION = 32512

TRANSPARENT = 1
PS_SOLID = 0
FW_NORMAL = 400
FW_SEMIBOLD = 600
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5
DEFAULT_PITCH = 0
DT_LEFT = 0x0000
DT_CENTER = 0x0001
DT_RIGHT = 0x0002
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_END_ELLIPSIS = 0x8000
ODS_SELECTED = 0x0001
ODS_DISABLED = 0x0004
ODS_FOCUS = 0x0010

EM_SETSEL = 0x00B1
EM_SCROLLCARET = 0x00B7
EM_SETREADONLY = 0x00CF
EM_SETMARGINS = 0x00D3
EC_LEFTMARGIN = 0x0001
EC_RIGHTMARGIN = 0x0002

WM_USER = 0x0400
UDM_SETBUDDY = WM_USER + 105
UDM_SETACCEL = WM_USER + 107
UDM_SETRANGE32 = WM_USER + 111
UDM_SETPOS32 = WM_USER + 113
UDS_SETBUDDYINT = 0x0002
UDS_ARROWKEYS = 0x0020
UDS_NOTHOUSANDS = 0x0080
CB_ADDSTRING = 0x0143
CB_GETCURSEL = 0x0147
CB_SETCURSEL = 0x014E
CBN_SELCHANGE = 1
VK_BACK = 0x08

EN_KILLFOCUS = 0x0200

ID_URL = 101
ID_OUTPUT = 102
ID_TIMEOUT = 103
ID_TIMEOUT_SPIN = 104
ID_OUTPUT_PAGE = 106
ID_BROWSER_SESSION_LABEL = 107
ID_BROWSER_SESSION = 108

ID_FULL = 201
ID_SCROLL = 202
ID_SCREENSHOTS = 203
ID_DESKTOP = 204
ID_MOBILE = 205
ID_VISIBLE_BROWSER = 206

ID_CAPTURE = 301
ID_CANCEL = 302
ID_OPEN = 303
ID_BROWSE = 304
ID_OUTPUT_OPEN = 305
ID_OUTPUT_BROWSE = 306
ID_GITHUB_LINK = 307
ID_OPEN_SESSION = 308

ID_STATUS = 401
ID_LOG = 402
ID_PROGRESS = 403

ID_NAV_CAPTURE = 501
ID_NAV_OUTPUT = 503
ID_NAV_ABOUT = 504

CHECK_IDS = {
    ID_FULL,
    ID_SCROLL,
    ID_SCREENSHOTS,
    ID_DESKTOP,
    ID_MOBILE,
    ID_VISIBLE_BROWSER,
}
NAV_IDS = {
    ID_NAV_CAPTURE: "capture",
    ID_NAV_OUTPUT: "output",
    ID_NAV_ABOUT: "about",
}

UI_REVISION = "2026-10-05-reliability-refactor-v7"
PRODUCT_NAME = "CaptureWebsite"
GITHUB_URL = "https://github.com/starlixdev/Capture-Website"
SESSION_CHOICES: tuple[tuple[str, BrowserSessionMode], ...] = (
    ("Isolated", "isolated"),
    ("Persistent Chrome", "persistent_chrome"),
    ("Persistent Edge", "persistent_edge"),
)

PROGRESS_BY_PHASE = {
    "prerequisites": 5,
    "capture": 18,
    "behavior": 42,
    "verification": 55,
    "extract": 68,
    "metadata": 82,
    "package": 92,
    "complete": 100,
}


def _rgb(red: int, green: int, blue: int) -> int:
    return red | (green << 8) | (blue << 16)


COLOR_BG = _rgb(22, 24, 29)
COLOR_BG_ALT = _rgb(19, 23, 28)
COLOR_INPUT = _rgb(30, 34, 41)
COLOR_INPUT_DARK = _rgb(28, 32, 39)
COLOR_BORDER = _rgb(55, 61, 69)
COLOR_SEPARATOR = _rgb(43, 48, 55)
COLOR_TEXTURE = _rgb(23, 26, 31)
COLOR_TEXT = _rgb(243, 245, 248)
COLOR_TEXT_STRONG = _rgb(255, 255, 255)
COLOR_MUTED = _rgb(186, 193, 203)
COLOR_DIM = _rgb(151, 160, 172)
COLOR_BLUE = _rgb(41, 116, 254)
COLOR_BLUE_PRESSED = _rgb(35, 102, 225)
COLOR_LINK = _rgb(92, 156, 255)
COLOR_BUTTON = _rgb(58, 63, 70)
COLOR_BUTTON_PRESSED = _rgb(49, 54, 61)
COLOR_DISABLED = _rgb(41, 47, 55)
COLOR_DISABLED_TEXT = _rgb(123, 132, 143)


if sys.platform == "win32":
    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [
            ("hdc", wintypes.HDC),
            ("fErase", wintypes.BOOL),
            ("rcPaint", RECT),
            ("fRestore", wintypes.BOOL),
            ("fIncUpdate", wintypes.BOOL),
            ("rgbReserved", ctypes.c_byte * 32),
        ]

    class DRAWITEMSTRUCT(ctypes.Structure):
        _fields_ = [
            ("CtlType", wintypes.UINT),
            ("CtlID", wintypes.UINT),
            ("itemID", wintypes.UINT),
            ("itemAction", wintypes.UINT),
            ("itemState", wintypes.UINT),
            ("hwndItem", wintypes.HWND),
            ("hDC", wintypes.HDC),
            ("rcItem", RECT),
            ("itemData", ULONG_PTR),
        ]

    class UDACCEL(ctypes.Structure):
        _fields_ = [("nSec", wintypes.UINT), ("nInc", wintypes.UINT)]

    class INITCOMMONCONTROLSEX(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("dwICC", wintypes.DWORD)]

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.LoadCursorW.restype = wintypes.HANDLE
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.WORD
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.SendMessageW.restype = LRESULT
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT), wintypes.BOOL]
    user32.BeginPaint.restype = wintypes.HDC
    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
    user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), wintypes.UINT]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.HideCaret.argtypes = [wintypes.HWND]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreatePen.restype = wintypes.HANDLE
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.CreateFontW.restype = wintypes.HANDLE
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
    ]
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
    gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPPOINT]
    gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

    dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    uxtheme.SetWindowTheme.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    comctl32.InitCommonControlsEx.argtypes = [ctypes.POINTER(INITCOMMONCONTROLSEX)]



_ACTIVE_APP: "SiteCaptureApp | None" = None


def _low_word(value: int) -> int:
    return value & 0xFFFF


def _high_word(value: int) -> int:
    return (value >> 16) & 0xFFFF


def _window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    app = _ACTIVE_APP
    if app is not None:
        try:
            handled = app.on_message(hwnd, message, wparam, lparam)
            if handled is not None:
                return handled
        except Exception as exc:
            app.show_error(str(exc))
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)  # type: ignore[union-attr]


if sys.platform == "win32":
    _WNDPROC = WNDPROC(_window_proc)
else:
    _WNDPROC = None


class SiteCaptureApp:
    # Keep the same practical window footprint as the original GUI while
    # rendering the reference design proportionally inside it.
    BASE_WIDTH = 700
    BASE_HEIGHT = 600
    UI_SCALE = 0.645
    SIDEBAR_WIDTH = 216
    CONTENT_X = 248
    CONTENT_RIGHT_MARGIN = 43

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("The CaptureWebsite GUI is available on Windows. Use the CLI on this platform.")
        self._enable_dpi_awareness()
        self.dpi_scale = self._system_scale()
        self.render_scale = self.dpi_scale * self.UI_SCALE
        self.instance = kernel32.GetModuleHandleW(None)
        self.hwnd: int | None = None
        self.controls: dict[int, int] = {}
        self.page_controls: dict[str, list[int]] = {"capture": [], "output": [], "about": []}
        self.text_colors: dict[int, int] = {}
        self.check_state = {
            ID_FULL: True,
            ID_SCROLL: True,
            ID_SCREENSHOTS: True,
            ID_DESKTOP: True,
            ID_MOBILE: False,
            ID_VISIBLE_BROWSER: True,
        }
        self.active_page = "capture"
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._session_thread: threading.Thread | None = None
        self._result: CaptureResult | None = None
        self._closing = False
        self._com_initialized = False
        self._log_guard_hwnd: int | None = None
        self._fonts: dict[str, int] = {}
        self._brushes: dict[str, int] = {}
        self._frames: dict[int, tuple[str, int, int, int, int, int, int]] = {}
        self.progress_value = 0
        self.output_dir = load_output_preference()
        self.browser_session: BrowserSessionMode = load_browser_session_preference()
        self._build_gdi_resources()

    @staticmethod
    def _enable_dpi_awareness() -> None:
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

    @staticmethod
    def _system_scale() -> float:
        try:
            dpi = int(user32.GetDpiForSystem())
            if dpi > 0:
                return dpi / 96.0
        except (AttributeError, OSError):
            pass
        return 1.0

    def _s(self, value: float) -> int:
        """Scale reference-design coordinates into device pixels."""
        return int(round(value * self.render_scale))

    def _window_s(self, value: float) -> int:
        """Scale the outer window size by DPI only, matching the old app footprint."""
        return int(round(value * self.dpi_scale))

    def _build_gdi_resources(self) -> None:
        self._brushes = {
            "bg": gdi32.CreateSolidBrush(COLOR_BG),
            "sidebar": gdi32.CreateSolidBrush(COLOR_BG_ALT),
            "input": gdi32.CreateSolidBrush(COLOR_INPUT),
            "input_dark": gdi32.CreateSolidBrush(COLOR_INPUT_DARK),
            "button": gdi32.CreateSolidBrush(COLOR_BUTTON),
            "blue": gdi32.CreateSolidBrush(COLOR_BLUE),
        }
        self._fonts = {
            "body": self._font(16, FW_NORMAL),
            "body_semibold": self._font(16, FW_SEMIBOLD),
            "input": self._font(18, FW_NORMAL),
            "nav": self._font(17, FW_NORMAL),
            "title": self._font(30, FW_SEMIBOLD),
            "button": self._font(16, FW_NORMAL),
            "button_primary": self._font(17, FW_SEMIBOLD),
            "button_large": self._font(17, FW_NORMAL),
            "section_large": self._font(18, FW_SEMIBOLD),
            "about_body": self._font(18, FW_NORMAL),
            "about_section": self._font(20, FW_SEMIBOLD),
            "small": self._font(14, FW_NORMAL),
            "link": self._font(17, FW_NORMAL, underline=True),
            "nav_icon": self._font(24, FW_NORMAL, face="Segoe Fluent Icons"),
            "checkbox_icon": self._font(29, FW_NORMAL, face="Segoe Fluent Icons"),
        }

    def _font(self, pixels: int, weight: int, *, face: str = "Segoe UI", underline: bool = False) -> int:
        return gdi32.CreateFontW(
            -self._s(pixels),
            0,
            0,
            0,
            weight,
            False,
            underline,
            False,
            DEFAULT_CHARSET,
            OUT_DEFAULT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY,
            DEFAULT_PITCH,
            face,
        )

    def run(self, *, smoke_test: bool = False) -> int:
        global _ACTIVE_APP
        _ACTIVE_APP = self
        try:
            self._initialize_common_controls()
            self._initialize_com()
            class_name = "SiteCaptureNativeWindowV2"
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = _WNDPROC
            window_class.hInstance = self.instance
            window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
            window_class.hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            window_class.hbrBackground = self._brushes["bg"]
            window_class.lpszClassName = class_name
            atom = user32.RegisterClassW(ctypes.byref(window_class))
            if not atom and ctypes.get_last_error() != 1410:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = user32.CreateWindowExW(
                0,
                class_name,
                PRODUCT_NAME,
                WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN | (0 if smoke_test else WS_VISIBLE),
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                self._window_s(self.BASE_WIDTH),
                self._window_s(self.BASE_HEIGHT),
                None,
                None,
                self.instance,
                None,
            )
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._apply_dark_window_theme()
            self._build_controls()
            self._layout_controls()
            self._switch_page("capture")
            if smoke_test:
                self._smoke_test_log_readonly()
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            else:
                user32.ShowWindow(self.hwnd, SW_SHOW)
                user32.UpdateWindow(self.hwnd)
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            return int(message.wParam)
        finally:
            _ACTIVE_APP = None
            self._release_log_readonly_guard()
            if self.hwnd:
                self._destroy_main_window()
            if self._com_initialized:
                ole32.CoUninitialize()
                self._com_initialized = False

    def _initialize_common_controls(self) -> None:
        controls = INITCOMMONCONTROLSEX()
        controls.dwSize = ctypes.sizeof(INITCOMMONCONTROLSEX)
        controls.dwICC = 0x00000010 | 0x00000020
        comctl32.InitCommonControlsEx(ctypes.byref(controls))

    def _initialize_com(self) -> None:
        try:
            result = ole32.CoInitializeEx(None, 0x2)
            self._com_initialized = result in (0, 1)
        except OSError:
            self._com_initialized = False

    def _apply_dark_window_theme(self) -> None:
        if not self.hwnd:
            return
        try:
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                if dwmapi.DwmSetWindowAttribute(
                    self.hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                ) == 0:
                    break
            caption = wintypes.DWORD(COLOR_BG_ALT)
            text = wintypes.DWORD(COLOR_TEXT)
            border = wintypes.DWORD(COLOR_SEPARATOR)
            for attribute, value in ((35, caption), (36, text), (34, border)):
                dwmapi.DwmSetWindowAttribute(
                    self.hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
        except (AttributeError, OSError):
            pass
        try:
            uxtheme.SetWindowTheme(self.hwnd, "DarkMode_Explorer", None)
        except (AttributeError, OSError):
            pass

    def _build_controls(self) -> None:
        self._nav_button(ID_NAV_CAPTURE, "Capture")
        self._nav_button(ID_NAV_OUTPUT, "Output")
        self._nav_button(ID_NAV_ABOUT, "About")

        default_output = str(self.output_dir)

        self._page_static("capture", "Capture Website", 0, 0, 0, 0, font="title")
        self._page_static("capture", "Website URL", 0, 0, 0, 0, font="body_semibold")
        self._edit(ID_URL, "https://example.com", page="capture")
        self._page_static("capture", "Output folder", 0, 0, 0, 0, font="body_semibold")
        self._edit(ID_OUTPUT, default_output, page="capture")
        self._button(ID_BROWSE, "Browse", page="capture")
        self._page_static("capture", "Time limit (seconds)", 0, 0, 0, 0, font="body_semibold")
        self._edit(ID_TIMEOUT, "120", page="capture")
        self._native_timeout_stepper(page="capture")
        self._check(ID_FULL, "Capture full page", page="capture")
        self._check(ID_SCROLL, "Scroll through page", page="capture")
        self._check(ID_SCREENSHOTS, "Capture images", page="capture")
        self._check(ID_DESKTOP, "Computer profile", page="capture")
        self._check(ID_MOBILE, "Mobile profile", page="capture")
        self._check(ID_VISIBLE_BROWSER, "Compatibility: open in visible browser", page="capture")
        self._button(ID_CAPTURE, "START CAPTURE", page="capture", primary=True)
        self._button(ID_CANCEL, "Cancel", page="capture")
        self._enable(ID_CANCEL, False)
        self._page_static("capture", "Progress", 0, 0, 0, 0, font="body_semibold")
        self.controls[ID_STATUS] = self._create(
            "STATIC", "Ready", SS_RIGHT | SS_NOPREFIX, 0, 0, 0, 0, ID_STATUS, font="body", color=COLOR_MUTED
        )
        self.page_controls["capture"].append(self.controls[ID_STATUS])
        self.controls[ID_PROGRESS] = self._create(
            "BUTTON", "", BS_OWNERDRAW, 0, 0, 0, 0, ID_PROGRESS, font=None
        )
        self.page_controls["capture"].append(self.controls[ID_PROGRESS])
        self.controls[ID_LOG] = self._create(
            "EDIT",
            "",
            WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL | ES_READONLY,
            0,
            0,
            0,
            0,
            ID_LOG,
            font="small",
        )
        self.page_controls["capture"].append(self.controls[ID_LOG])
        self._style_edit(self.controls[ID_LOG], read_only=True)
        self._install_log_readonly_guard(self.controls[ID_LOG])
        self._button(ID_OPEN, "Open folder", page="capture")
        self._enable(ID_OPEN, False)

        self.controls[ID_BROWSER_SESSION_LABEL] = self._create(
            "STATIC", "Browser session", SS_LEFT | SS_NOPREFIX,
            0, 0, 0, 0, ID_BROWSER_SESSION_LABEL, font="body_semibold"
        )
        self.page_controls["capture"].append(self.controls[ID_BROWSER_SESSION_LABEL])
        self._browser_session_combo(page="capture")
        self._button(ID_OPEN_SESSION, "Open session", page="capture")
        self._update_session_controls()

        self._page_static("output", "Output", 0, 0, 0, 0, font="title")
        self._page_static("output", "Output folder", 0, 0, 0, 0, font="section_large")
        self._edit(ID_OUTPUT_PAGE, default_output, page="output")
        self._button(ID_OUTPUT_BROWSE, "Browse", page="output", font="button_large")
        self._button(ID_OUTPUT_OPEN, "Open output folder", page="output", font="button_large")

        self._page_static("about", "About", 0, 0, 0, 0, font="title")
        self._page_static(
            "about",
            "CaptureWebsite captures websites through an isolated or CaptureWebsite-owned persistent Microsoft Edge or "
            "Google Chrome session. It records the resources loaded by the browser and saves them locally using the "
            "selected capture settings.",
            0, 0, 0, 0, font="about_body", color=COLOR_MUTED,
        )
        self._page_static("about", "Capture", 0, 0, 0, 0, font="about_section")
        self._page_static(
            "about",
            "Enter a website URL and choose how it should be captured. CaptureWebsite opens the page in the selected "
            "browser session and collects the resources requested while the page loads.",
            0, 0, 0, 0, font="about_body", color=COLOR_MUTED,
        )
        self._page_static("about", "Output", 0, 0, 0, 0, font="about_section")
        self._page_static(
            "about",
            "Choose where captured websites are saved. The generated files are kept together so the captured result "
            "can be accessed and managed outside the application.",
            0, 0, 0, 0, font="about_body", color=COLOR_MUTED,
        )
        self._page_static("about", "Command Line", 0, 0, 0, 0, font="about_section")
        self._page_static(
            "about",
            "CaptureWebsite can also run from the command line. The GUI and CLI use the same capture engine and output format.",
            0, 0, 0, 0, font="about_body", color=COLOR_MUTED,
        )
        self._page_static("about", "GitHub", 0, 0, 0, 0, font="about_section")
        self._page_static(
            "about", "Source code, releases and issue tracking:", 0, 0, 0, 0,
            font="about_body", color=COLOR_MUTED
        )
        self._link(ID_GITHUB_LINK, GITHUB_URL, page="about")
        self._page_static("about", f"CaptureWebsite {__version__}", 0, 0, 0, 0, color=COLOR_DIM)

    def _create(
        self,
        class_name: str,
        text: str,
        style: int,
        x: int,
        y: int,
        width: int,
        height: int,
        control_id: int,
        *,
        font: str | None = "body",
        color: int = COLOR_TEXT,
    ) -> int:
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            text,
            WS_CHILD | WS_VISIBLE | style,
            self._s(x),
            self._s(y),
            self._s(width),
            self._s(height),
            self.hwnd,
            ctypes.c_void_p(control_id),
            self.instance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        if font is not None:
            user32.SendMessageW(hwnd, WM_SETFONT, self._fonts[font], True)
        self.text_colors[hwnd] = color
        return hwnd

    def _page_static(
        self,
        page: str,
        text: str,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        font: str = "body",
        color: int = COLOR_TEXT,
    ) -> int:
        hwnd = self._create(
            "STATIC", text, SS_LEFT | SS_NOPREFIX, x, y, width, height, 0, font=font, color=color
        )
        self.page_controls[page].append(hwnd)
        return hwnd

    def _edit(self, control_id: int, text: str, *, page: str) -> None:
        hwnd = self._create(
            "EDIT", text, WS_TABSTOP | ES_AUTOHSCROLL, 0, 0, 0, 0, control_id, font="input"
        )
        self.controls[control_id] = hwnd
        self.page_controls[page].append(hwnd)
        self._style_edit(hwnd)

    def _style_edit(self, hwnd: int, *, read_only: bool = False) -> None:
        try:
            theme = "DarkMode_Explorer" if read_only else "DarkMode_CFD"
            uxtheme.SetWindowTheme(hwnd, theme, None)
        except OSError:
            pass
        margin = self._s(10)
        packed = (margin & 0xFFFF) | ((margin & 0xFFFF) << 16)
        user32.SendMessageW(hwnd, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, packed)
        if read_only:
            user32.SendMessageW(hwnd, EM_SETREADONLY, True, 0)
            self.text_colors[hwnd] = COLOR_MUTED

    def _install_log_readonly_guard(self, hwnd: int) -> None:
        install_readonly_edit_guard(hwnd)
        self._log_guard_hwnd = hwnd

    def _release_log_readonly_guard(self) -> None:
        if self._log_guard_hwnd is None:
            return
        remove_readonly_edit_guard(self._log_guard_hwnd)
        self._log_guard_hwnd = None

    def _browser_session_combo(self, *, page: str) -> None:
        hwnd = self._create(
            "COMBOBOX",
            "",
            WS_TABSTOP | WS_VSCROLL | CBS_DROPDOWNLIST | CBS_HASSTRINGS,
            0, 0, 0, 0, ID_BROWSER_SESSION, font="input",
        )
        self.controls[ID_BROWSER_SESSION] = hwnd
        self.page_controls[page].append(hwnd)
        try:
            uxtheme.SetWindowTheme(hwnd, "DarkMode_CFD", None)
        except OSError:
            pass
        selected_index = 0
        for index, (label, mode) in enumerate(SESSION_CHOICES):
            buffer = ctypes.create_unicode_buffer(label)
            user32.SendMessageW(hwnd, CB_ADDSTRING, 0, ctypes.addressof(buffer))
            if mode == self.browser_session:
                selected_index = index
        user32.SendMessageW(hwnd, CB_SETCURSEL, selected_index, 0)

    def _selected_browser_session(self) -> BrowserSessionMode:
        hwnd = self.controls.get(ID_BROWSER_SESSION)
        if not hwnd:
            return self.browser_session
        index = int(user32.SendMessageW(hwnd, CB_GETCURSEL, 0, 0))
        if 0 <= index < len(SESSION_CHOICES):
            return SESSION_CHOICES[index][1]
        return "isolated"

    def _browser_session_changed(self) -> None:
        mode = self._selected_browser_session()
        self.browser_session = mode
        try:
            save_browser_session_preference(mode)
        except OSError as exc:
            self.show_error(f"CaptureWebsite could not save the browser-session preference:\n{exc}")
        self._update_session_controls()

    def _update_session_controls(self) -> None:
        capture_running = bool(self._worker and self._worker.is_alive())
        session_running = bool(self._session_thread and self._session_thread.is_alive())
        self._enable(ID_BROWSER_SESSION, not capture_running and not session_running)
        self._enable(
            ID_OPEN_SESSION,
            self._selected_browser_session() != "isolated" and not capture_running and not session_running,
        )

    def _open_persistent_session(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if self._session_thread and self._session_thread.is_alive():
            return
        mode = self._selected_browser_session()
        if mode == "isolated":
            return
        self.browser_session = mode
        try:
            save_browser_session_preference(mode)
        except OSError as exc:
            self.show_error(f"CaptureWebsite could not save the browser-session preference:\n{exc}")
            return
        self._enable(ID_CAPTURE, False)
        self._update_session_controls()
        label = dict((value, text) for text, value in SESSION_CHOICES)[mode]
        self._set_status(
            f"Opening {label}. Log in normally, then close the browser window when finished.",
            append=True,
        )

        def work() -> None:
            try:
                open_persistent_browser_session(mode)
                self._post_event("session_closed", mode)
            except Exception as exc:
                self._post_event("session_error", exc)

        self._session_thread = threading.Thread(
            target=work, name="capturewebsite-persistent-session", daemon=True
        )
        self._session_thread.start()
        self._update_session_controls()

    def _check(self, control_id: int, text: str, *, page: str) -> None:
        hwnd = self._create(
            "BUTTON", text, WS_TABSTOP | BS_OWNERDRAW, 0, 0, 0, 0, control_id, font="body"
        )
        self.controls[control_id] = hwnd
        self.page_controls[page].append(hwnd)

    def _native_timeout_stepper(self, *, page: str) -> None:
        hwnd = self._create(
            "msctls_updown32",
            "",
            UDS_SETBUDDYINT | UDS_ARROWKEYS | UDS_NOTHOUSANDS,
            0, 0, 0, 0, ID_TIMEOUT_SPIN, font=None,
        )
        self.controls[ID_TIMEOUT_SPIN] = hwnd
        self.page_controls[page].append(hwnd)
        try:
            uxtheme.SetWindowTheme(hwnd, "DarkMode_Explorer", None)
        except OSError:
            pass
        user32.SendMessageW(hwnd, UDM_SETBUDDY, self.controls[ID_TIMEOUT], 0)
        user32.SendMessageW(hwnd, UDM_SETRANGE32, 10, 3600)
        accel = UDACCEL(0, 10)
        user32.SendMessageW(hwnd, UDM_SETACCEL, 1, ctypes.addressof(accel))
        user32.SendMessageW(hwnd, UDM_SETPOS32, 0, 120)

    def _link(self, control_id: int, text: str, *, page: str) -> None:
        hwnd = self._create(
            "BUTTON", text, WS_TABSTOP | BS_OWNERDRAW, 0, 0, 0, 0, control_id, font="link", color=COLOR_LINK
        )
        self.controls[control_id] = hwnd
        self.page_controls[page].append(hwnd)

    def _button(
        self,
        control_id: int,
        text: str,
        *,
        page: str,
        primary: bool = False,
        font: str = "button",
    ) -> None:
        hwnd = self._create(
            "BUTTON", text, WS_TABSTOP | BS_OWNERDRAW, 0, 0, 0, 0, control_id, font=font
        )
        self.controls[control_id] = hwnd
        self.page_controls[page].append(hwnd)
        if primary:
            self.text_colors[hwnd] = COLOR_TEXT

    def _nav_button(self, control_id: int, text: str) -> None:
        self.controls[control_id] = self._create(
            "BUTTON", text, WS_TABSTOP | BS_OWNERDRAW, 0, 0, 0, 0, control_id, font="nav", color=COLOR_MUTED
        )

    def _layout_controls(self) -> None:
        if not self.hwnd:
            return
        rect = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        logical_width = max(1, int((rect.right - rect.left) / self.render_scale))
        logical_height = max(1, int((rect.bottom - rect.top) / self.render_scale))
        main_width = max(620, logical_width - self.CONTENT_X - self.CONTENT_RIGHT_MARGIN)
        self._frames.clear()

        self._move(ID_NAV_CAPTURE, 0, 16, self.SIDEBAR_WIDTH, 54)
        self._move(ID_NAV_OUTPUT, 0, 78, self.SIDEBAR_WIDTH, 54)
        self._move(ID_NAV_ABOUT, 0, 140, self.SIDEBAR_WIDTH, 54)

        capture = self.page_controls["capture"]
        if len(capture) >= 18:
            self._move_hwnd(capture[0], self.CONTENT_X, 25, main_width, 38)
            self._move_hwnd(capture[1], self.CONTENT_X, 99, 220, 24)
            self._place_edit(ID_URL, "capture", self.CONTENT_X, 124, main_width, 42)
            self._move_hwnd(capture[3], self.CONTENT_X, 191, 220, 24)
            self._place_edit(ID_OUTPUT, "capture", self.CONTENT_X, 215, max(180, main_width - 120), 44)
            self._move(ID_BROWSE, self.CONTENT_X + main_width - 110, 215, 110, 44)
            self._move_hwnd(capture[6], self.CONTENT_X, 279, 220, 24)
            self._place_edit(ID_TIMEOUT, "capture", self.CONTENT_X, 304, 216, 40, right_reserved=30)
            self._move(ID_TIMEOUT_SPIN, self.CONTENT_X + 187, 305, 28, 38)

        session_x = self.CONTENT_X + 260
        session_button_width = 136
        session_combo_width = max(190, main_width - 260 - session_button_width - 12)
        self._move(ID_BROWSER_SESSION_LABEL, session_x, 279, 200, 24)
        # ComboBox height includes its drop-down list; the closed field keeps the
        # normal native height while the list has enough room for all three modes.
        self._move(ID_BROWSER_SESSION, session_x, 304, session_combo_width, 150)
        self._move(
            ID_OPEN_SESSION,
            session_x + session_combo_width + 12,
            304,
            session_button_width,
            40,
        )

        left_width = min(330, max(260, int(main_width * 0.48)))
        right_x = self.CONTENT_X + max(384, int(main_width * 0.5))
        right_width = max(240, self.CONTENT_X + main_width - right_x)
        options_y = 374
        self._move(ID_FULL, self.CONTENT_X, options_y, left_width, 34)
        self._move(ID_SCROLL, self.CONTENT_X, options_y + 40, left_width, 34)
        self._move(ID_SCREENSHOTS, self.CONTENT_X, options_y + 80, left_width, 34)
        self._move(ID_DESKTOP, right_x, options_y, right_width, 34)
        self._move(ID_MOBILE, right_x, options_y + 40, right_width, 34)
        self._move(ID_VISIBLE_BROWSER, right_x, options_y + 80, right_width, 34)

        self._move(ID_CAPTURE, self.CONTENT_X, 516, max(260, main_width - 180), 46)
        self._move(ID_CANCEL, self.CONTENT_X + main_width - 170, 516, 170, 46)

        progress_y = 598
        log_y = 653
        open_y = min(806, max(738, logical_height - 59))
        log_height = max(42, open_y - log_y - 12)
        progress_label_hwnd = capture[17] if len(capture) > 17 else None
        if progress_label_hwnd:
            self._move_hwnd(progress_label_hwnd, self.CONTENT_X, progress_y, 180, 24)
        self._move(ID_STATUS, self.CONTENT_X + main_width - 220, progress_y, 220, 24)
        self._move(ID_PROGRESS, self.CONTENT_X, 623, main_width, 18)
        self._place_panel(ID_LOG, "capture", self.CONTENT_X, log_y, main_width, log_height)
        self._move(ID_OPEN, self.CONTENT_X, open_y, 134, 42)

        output = self.page_controls["output"]
        if output:
            self._move_hwnd(output[0], self.CONTENT_X, 25, main_width, 38)
            self._move_hwnd(output[1], self.CONTENT_X, 112, main_width, 30)
            self._place_edit(
                ID_OUTPUT_PAGE,
                "output",
                self.CONTENT_X,
                151,
                max(180, main_width - 120),
                46,
            )
            self._move(ID_OUTPUT_BROWSE, self.CONTENT_X + main_width - 110, 151, 110, 46)
            self._move(ID_OUTPUT_OPEN, self.CONTENT_X, 226, 200, 44)

        about = self.page_controls["about"]
        if len(about) >= 12:
            self._move_hwnd(about[0], self.CONTENT_X, 25, main_width, 38)
            self._move_hwnd(about[1], self.CONTENT_X, 83, main_width, 70)
            self._move_hwnd(about[2], self.CONTENT_X, 171, main_width, 28)
            self._move_hwnd(about[3], self.CONTENT_X, 204, main_width, 62)
            self._move_hwnd(about[4], self.CONTENT_X, 282, main_width, 28)
            self._move_hwnd(about[5], self.CONTENT_X, 315, main_width, 62)
            self._move_hwnd(about[6], self.CONTENT_X, 393, main_width, 28)
            self._move_hwnd(about[7], self.CONTENT_X, 426, main_width, 58)
            self._move_hwnd(about[8], self.CONTENT_X, 500, main_width, 28)
            self._move_hwnd(about[9], self.CONTENT_X, 533, main_width, 28)
            self._move(ID_GITHUB_LINK, self.CONTENT_X, 561, main_width, 30)
            version_y = max(720, logical_height - 54)
            self._move_hwnd(about[11], self.CONTENT_X, version_y, main_width, 24)

        user32.InvalidateRect(self.hwnd, None, False)

    def _place_edit(
        self,
        control_id: int,
        page: str,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        right_reserved: int = 0,
    ) -> None:
        self._frames[control_id] = (page, x, y, width, height, COLOR_INPUT, COLOR_BORDER)
        text_height = min(30, max(20, height - 8))
        text_y = y + max(1, (height - text_height) // 2)
        edit_width = max(20, width - right_reserved - 2)
        self._move(control_id, x + 1, text_y, edit_width, text_height)

    def _place_panel(self, control_id: int, page: str, x: int, y: int, width: int, height: int) -> None:
        self._frames[control_id] = (page, x, y, width, height, COLOR_INPUT_DARK, COLOR_BORDER)
        self._move(control_id, x + 1, y + 1, max(1, width - 2), max(1, height - 2))

    def _move(self, control_id: int, x: int, y: int, width: int, height: int) -> None:
        hwnd = self.controls.get(control_id)
        if hwnd:
            self._move_hwnd(hwnd, x, y, width, height)

    def _move_hwnd(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        user32.MoveWindow(hwnd, self._s(x), self._s(y), self._s(width), self._s(height), True)

    def _switch_page(self, page: str) -> None:
        if page not in self.page_controls:
            return
        if page != self.active_page:
            source_id = ID_OUTPUT_PAGE if self.active_page == "output" else ID_OUTPUT
            self._commit_output_folder(source_id, notify=False)
        if page == "output":
            self._set_text(ID_OUTPUT_PAGE, self._text(ID_OUTPUT))
        elif self.active_page == "output":
            self._set_text(ID_OUTPUT, self._text(ID_OUTPUT_PAGE))
        self.active_page = page
        for name, handles in self.page_controls.items():
            visible = name == page
            for handle in handles:
                user32.ShowWindow(handle, SW_SHOW if visible else SW_HIDE)
        for control_id in NAV_IDS:
            self._invalidate_control(control_id)
        if self.hwnd:
            user32.InvalidateRect(self.hwnd, None, False)

    def _destroy_main_window(self) -> None:
        self._release_log_readonly_guard()
        if self.hwnd:
            hwnd = self.hwnd
            self.hwnd = None
            user32.DestroyWindow(hwnd)

    def on_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> int | None:
        if message == WM_COMMAND:
            control_id = _low_word(wparam)
            notification = _high_word(wparam)
            if control_id in NAV_IDS:
                self._switch_page(NAV_IDS[control_id])
            elif control_id == ID_BROWSER_SESSION and notification == CBN_SELCHANGE:
                self._browser_session_changed()
            elif control_id in CHECK_IDS:
                self.check_state[control_id] = not self.check_state[control_id]
                self._invalidate_control(control_id)
            elif control_id == ID_CAPTURE:
                self._start()
            elif control_id == ID_CANCEL:
                self._request_cancel()
            elif control_id == ID_OPEN:
                self._open_folder()
            elif control_id == ID_OUTPUT_OPEN:
                self._open_folder(use_selected_output=True)
            elif control_id == ID_OPEN_SESSION:
                self._open_persistent_session()
            elif control_id in {ID_BROWSE, ID_OUTPUT_BROWSE}:
                self._browse_output_folder(control_id)
            elif control_id in {ID_OUTPUT, ID_OUTPUT_PAGE} and _high_word(wparam) == EN_KILLFOCUS:
                self._commit_output_folder(control_id, notify=False)
            elif control_id == ID_GITHUB_LINK:
                self._open_github()
            return 0
        if message == WM_DRAWITEM:
            item = ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents
            self._draw_owner_control(item)
            return 1
        if message == WM_CTLCOLORSTATIC:
            hdc = wparam
            child = lparam
            if child == self.controls.get(ID_LOG):
                gdi32.SetBkMode(hdc, 2)
                gdi32.SetTextColor(hdc, COLOR_MUTED)
                gdi32.SetBkColor(hdc, COLOR_INPUT_DARK)
                return self._brushes["input_dark"]
            gdi32.SetBkMode(hdc, TRANSPARENT)
            gdi32.SetTextColor(hdc, self.text_colors.get(child, COLOR_TEXT))
            return self._brushes["bg"]
        if message == WM_CTLCOLOREDIT:
            hdc = wparam
            child = lparam
            gdi32.SetBkMode(hdc, 2)
            gdi32.SetTextColor(hdc, self.text_colors.get(child, COLOR_TEXT))
            if child == self.controls.get(ID_LOG):
                gdi32.SetBkColor(hdc, COLOR_INPUT_DARK)
                return self._brushes["input_dark"]
            gdi32.SetBkColor(hdc, COLOR_INPUT)
            return self._brushes["input"]
        if message == WM_PAINT:
            self._paint_background()
            return 0
        if message == WM_ERASEBKGND:
            return 1
        if message == WM_SIZE:
            self._layout_controls()
            return 0
        if message == WM_APP_EVENT:
            self._drain_events()
            return 0
        if message == WM_CLOSE:
            source_id = ID_OUTPUT_PAGE if self.active_page == "output" else ID_OUTPUT
            self._commit_output_folder(source_id, notify=False)
            if self._session_thread and self._session_thread.is_alive():
                self.show_error(
                    "Close the CaptureWebsite persistent browser window before exiting the application."
                )
                return 0
            if self._worker and self._worker.is_alive():
                self._closing = True
                self._request_cancel()
                self._set_status("Stopping the active capture before closing…", append=True)
            else:
                self._destroy_main_window()
            return 0
        if message == WM_DESTROY:
            self._release_log_readonly_guard()
            if self.hwnd == hwnd:
                self.hwnd = None
            user32.PostQuitMessage(0)
            return 0
        return None

    def _paint_background(self) -> None:
        if not self.hwnd:
            return
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            rect = RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(rect))
            user32.FillRect(hdc, ctypes.byref(rect), self._brushes["bg"])
            sidebar = RECT(0, 0, self._s(self.SIDEBAR_WIDTH), rect.bottom)
            user32.FillRect(hdc, ctypes.byref(sidebar), self._brushes["sidebar"])

            texture_pen = gdi32.CreatePen(PS_SOLID, 1, COLOR_TEXTURE)
            old_pen = gdi32.SelectObject(hdc, texture_pen)
            spacing = max(1, self._s(84))
            start_x = self._s(self.SIDEBAR_WIDTH)
            for x in range(start_x - rect.bottom, rect.right + rect.bottom, spacing):
                gdi32.MoveToEx(hdc, x, rect.bottom, None)
                gdi32.LineTo(hdc, x + rect.bottom, 0)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.DeleteObject(texture_pen)

            separator_pen = gdi32.CreatePen(PS_SOLID, 1, COLOR_SEPARATOR)
            old_pen = gdi32.SelectObject(hdc, separator_pen)
            sidebar_x = self._s(self.SIDEBAR_WIDTH)
            gdi32.MoveToEx(hdc, sidebar_x, 0, None)
            gdi32.LineTo(hdc, sidebar_x, rect.bottom)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.DeleteObject(separator_pen)

            for page, x, y, width, height, fill_color, border_color in self._frames.values():
                if page != self.active_page:
                    continue
                frame = RECT(self._s(x), self._s(y), self._s(x + width), self._s(y + height))
                border_brush = gdi32.CreateSolidBrush(border_color)
                user32.FillRect(hdc, ctypes.byref(frame), border_brush)
                gdi32.DeleteObject(border_brush)
                inset = max(1, self._s(1))
                inner = RECT(
                    frame.left + inset,
                    frame.top + inset,
                    frame.right - inset,
                    frame.bottom - inset,
                )
                fill_brush = gdi32.CreateSolidBrush(fill_color)
                user32.FillRect(hdc, ctypes.byref(inner), fill_brush)
                gdi32.DeleteObject(fill_brush)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    def _draw_owner_control(self, item: "DRAWITEMSTRUCT") -> None:
        control_id = int(item.CtlID)
        if control_id == ID_PROGRESS:
            self._draw_progress(item)
        elif control_id in NAV_IDS:
            self._draw_nav_button(item, control_id)
        elif control_id in CHECK_IDS:
            self._draw_checkbox(item, control_id)
        elif control_id == ID_GITHUB_LINK:
            self._draw_link_button(item)
        else:
            self._draw_action_button(item, control_id)

    def _draw_nav_button(self, item: "DRAWITEMSTRUCT", control_id: int) -> None:
        hdc = item.hDC
        rect = item.rcItem
        active = NAV_IDS[control_id] == self.active_page
        user32.FillRect(hdc, ctypes.byref(rect), self._brushes["sidebar"])
        if active:
            bar = RECT(rect.left + self._s(4), rect.top + self._s(4), rect.left + self._s(11), rect.bottom - self._s(4))
            blue = gdi32.CreateSolidBrush(COLOR_BLUE)
            user32.FillRect(hdc, ctypes.byref(bar), blue)
            gdi32.DeleteObject(blue)
        color = COLOR_TEXT if active else COLOR_MUTED
        self._draw_nav_icon(hdc, control_id, rect, color)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, color)
        old_font = gdi32.SelectObject(hdc, self._fonts["nav"])
        text_rect = RECT(rect.left + self._s(75), rect.top, rect.right - self._s(8), rect.bottom)
        user32.DrawTextW(hdc, self._button_text(item.hwndItem), -1, ctypes.byref(text_rect), DT_LEFT | DT_VCENTER | DT_SINGLELINE)
        gdi32.SelectObject(hdc, old_font)

    def _draw_nav_icon(self, hdc: int, control_id: int, rect: "RECT", color: int) -> None:
        glyphs = {
            ID_NAV_CAPTURE: "\ue774",
            ID_NAV_OUTPUT: "\ue8b7",
            ID_NAV_ABOUT: "\uea1f",
        }
        glyph = glyphs.get(control_id, "")
        if not glyph:
            return
        left = rect.left + self._s(25)
        right = rect.left + self._s(57)
        icon_rect = RECT(left, rect.top, right, rect.bottom)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, color)
        old_font = gdi32.SelectObject(hdc, self._fonts["nav_icon"])
        user32.DrawTextW(
            hdc,
            glyph,
            -1,
            ctypes.byref(icon_rect),
            DT_CENTER | DT_VCENTER | DT_SINGLELINE,
        )
        gdi32.SelectObject(hdc, old_font)

    def _draw_checkbox(self, item: "DRAWITEMSTRUCT", control_id: int) -> None:
        hdc = item.hDC
        rect = item.rcItem
        user32.FillRect(hdc, ctypes.byref(rect), self._brushes["bg"])
        checked = self.check_state[control_id]

        icon_rect = RECT(rect.left, rect.top, rect.left + self._s(34), rect.bottom)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        old_icon_font = gdi32.SelectObject(hdc, self._fonts["checkbox_icon"])
        if checked:
            gdi32.SetTextColor(hdc, COLOR_BLUE)
            user32.DrawTextW(
                hdc, "\ue73b", -1, ctypes.byref(icon_rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE
            )
            gdi32.SetTextColor(hdc, COLOR_TEXT)
            user32.DrawTextW(
                hdc, "\ue73e", -1, ctypes.byref(icon_rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE
            )
        else:
            gdi32.SetTextColor(hdc, COLOR_DIM)
            user32.DrawTextW(
                hdc, "\ue739", -1, ctypes.byref(icon_rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE
            )
        gdi32.SelectObject(hdc, old_icon_font)

        gdi32.SetTextColor(hdc, COLOR_TEXT)
        old_font = gdi32.SelectObject(hdc, self._fonts["body"])
        text_rect = RECT(icon_rect.right + self._s(11), rect.top, rect.right, rect.bottom)
        user32.DrawTextW(
            hdc,
            self._button_text(item.hwndItem),
            -1,
            ctypes.byref(text_rect),
            DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
        )
        gdi32.SelectObject(hdc, old_font)

    def _draw_progress(self, item: "DRAWITEMSTRUCT") -> None:
        rect = item.rcItem
        user32.FillRect(item.hDC, ctypes.byref(rect), self._brushes["button"])
        value = max(0, min(100, int(self.progress_value)))
        if value <= 0:
            return
        width = max(1, ((rect.right - rect.left) * value) // 100)
        filled = RECT(rect.left, rect.top, rect.left + width, rect.bottom)
        user32.FillRect(item.hDC, ctypes.byref(filled), self._brushes["blue"])

    def _draw_action_button(self, item: "DRAWITEMSTRUCT", control_id: int) -> None:
        hdc = item.hDC
        rect = item.rcItem
        disabled = bool(item.itemState & ODS_DISABLED)
        pressed = bool(item.itemState & ODS_SELECTED)
        primary = control_id == ID_CAPTURE
        if disabled:
            fill_color = COLOR_DISABLED
            text_color = COLOR_DISABLED_TEXT
        elif primary:
            fill_color = COLOR_BLUE_PRESSED if pressed else COLOR_BLUE
            text_color = COLOR_TEXT_STRONG
        else:
            fill_color = COLOR_BUTTON_PRESSED if pressed else COLOR_BUTTON
            text_color = COLOR_TEXT
        user32.FillRect(hdc, ctypes.byref(rect), self._brushes["bg"])
        brush = gdi32.CreateSolidBrush(fill_color)
        pen = gdi32.CreatePen(PS_SOLID, 1, COLOR_BORDER if not primary else fill_color)
        old_brush = gdi32.SelectObject(hdc, brush)
        old_pen = gdi32.SelectObject(hdc, pen)
        gdi32.RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, self._s(3), self._s(3))
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, text_color)
        font_key = "button_primary" if primary else ("button_large" if control_id in {ID_OUTPUT_BROWSE, ID_OUTPUT_OPEN} else "button")
        old_font = gdi32.SelectObject(hdc, self._fonts[font_key])
        user32.DrawTextW(hdc, self._button_text(item.hwndItem), -1, ctypes.byref(rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
        gdi32.SelectObject(hdc, old_font)
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(brush)
        gdi32.DeleteObject(pen)

    def _draw_link_button(self, item: "DRAWITEMSTRUCT") -> None:
        hdc = item.hDC
        rect = item.rcItem
        user32.FillRect(hdc, ctypes.byref(rect), self._brushes["bg"])
        color = COLOR_MUTED if (item.itemState & ODS_DISABLED) else COLOR_LINK
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, color)
        old_font = gdi32.SelectObject(hdc, self._fonts["link"])
        user32.DrawTextW(
            hdc,
            self._button_text(item.hwndItem),
            -1,
            ctypes.byref(rect),
            DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
        )
        gdi32.SelectObject(hdc, old_font)

    @staticmethod
    def _button_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if self._session_thread and self._session_thread.is_alive():
            self.show_error("Close the open persistent browser session before starting a capture.")
            return
        self._commit_output_folder(ID_OUTPUT, notify=True)
        session_mode = self._selected_browser_session()
        try:
            save_browser_session_preference(session_mode)
            self.browser_session = session_mode
            config = CaptureConfig(
                url=self._text(ID_URL),
                output_dir=Path(self._text(ID_OUTPUT)),
                full=self._checked(ID_FULL),
                desktop=self._checked(ID_DESKTOP),
                mobile=self._checked(ID_MOBILE),
                timeout=int(self._text(ID_TIMEOUT)),
                scroll=self._checked(ID_SCROLL),
                screenshots=self._checked(ID_SCREENSHOTS),
                visible_browser=self._checked(ID_VISIBLE_BROWSER),
                browser_session=session_mode,
            )
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._cancel.clear()
        self._result = None
        self._enable(ID_CAPTURE, False)
        self._enable(ID_CANCEL, True)
        self._enable(ID_OPEN, False)
        self._update_session_controls()
        self._set_text(ID_LOG, "")
        self._set_progress_running(True)
        self._set_status("Starting capture…", append=True)

        def work() -> None:
            try:
                engine = CaptureEngine(
                    config,
                    progress=lambda phase, detail: self._post_event("progress", (phase, detail)),
                )
                self._post_event("done", engine.run(self._cancel))
            except Exception as exc:
                self._post_event("error", exc)

        self._worker = threading.Thread(target=work, name="sitecapture-gui-capture", daemon=True)
        self._worker.start()
        self._update_session_controls()

    def _post_event(self, kind: str, payload: object) -> None:
        self._events.put((kind, payload))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_EVENT, 0, 0)

    def _drain_events(self) -> None:
        terminal_event = False
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                phase, detail = payload  # type: ignore[misc]
                phase_key = str(phase).lower()
                self._set_progress_value(max(self.progress_value, PROGRESS_BY_PHASE.get(phase_key, self.progress_value)))
                label = str(phase).replace("_", " ").title()
                self._set_status(f"{label}: {detail}", append=True)
            elif kind == "done":
                terminal_event = True
                self._worker = None
                self._result = payload  # type: ignore[assignment]
                self._set_progress_value(100)
                self._finish_controls()
                self._set_status("Capture complete", append=True)
                self._append_log(f"ZIP: {self._result.zip_path}")
                self._enable(ID_OPEN, True)
            elif kind == "session_closed":
                self._session_thread = None
                self._enable(ID_CAPTURE, True)
                self._update_session_controls()
                self._set_status("Persistent browser session closed; login state was kept for future captures.", append=True)
            elif kind == "session_error":
                self._session_thread = None
                self._enable(ID_CAPTURE, True)
                self._update_session_controls()
                self._set_status("Persistent browser session could not be opened", append=True)
                if not self._closing:
                    self.show_error(str(payload))
            elif kind == "error":
                terminal_event = True
                self._worker = None
                self._finish_controls()
                self._set_status("Capture failed or was cancelled", append=True)
                if not self._closing:
                    self.show_error(str(payload))
        if self._closing and terminal_event:
            self._destroy_main_window()

    def _request_cancel(self) -> None:
        self._cancel.set()
        self._enable(ID_CANCEL, False)
        self._set_status(
            "Cancellation requested; closing the active browser and preserving captured records…",
            append=True,
        )

    def _finish_controls(self) -> None:
        self._set_progress_running(False)
        self._enable(ID_CAPTURE, True)
        self._enable(ID_CANCEL, False)
        self._update_session_controls()

    def _set_progress_running(self, running: bool) -> None:
        if running or self._result is None:
            self._set_progress_value(0)

    def _set_progress_value(self, value: int) -> None:
        self.progress_value = max(0, min(100, int(value)))
        self._invalidate_control(ID_PROGRESS)

    def _browse_output_folder(self, control_id: int) -> None:
        source_id = ID_OUTPUT_PAGE if control_id == ID_OUTPUT_BROWSE else ID_OUTPUT
        try:
            selected = choose_folder(self.hwnd, self._text(source_id), title="Choose an output folder")
        except OSError as exc:
            self.show_error(f"Could not open the Windows folder picker:\n{exc}")
            return
        if selected is not None:
            self._set_output_folder(selected, persist=True, notify=True)

    def _set_output_folder(self, value: str | Path, *, persist: bool, notify: bool) -> Path:
        resolved = Path(value).expanduser().resolve()
        text = str(resolved)
        self.output_dir = resolved
        self._set_text(ID_OUTPUT, text)
        self._set_text(ID_OUTPUT_PAGE, text)
        if persist:
            try:
                save_output_preference(resolved)
            except OSError as exc:
                if notify:
                    self.show_error(
                        "The output folder was selected, but CaptureWebsite could not save "
                        f"the preference for the next launch:\n{exc}"
                    )
        return resolved

    def _commit_output_folder(self, source_id: int, *, notify: bool) -> Path | None:
        value = self._text(source_id).strip()
        if not value:
            return None
        try:
            return self._set_output_folder(value, persist=True, notify=notify)
        except (OSError, ValueError) as exc:
            if notify:
                self.show_error(f"Invalid output folder:\n{exc}")
            return None

    def _open_github(self) -> None:
        try:
            os.startfile(GITHUB_URL)
        except OSError as exc:
            self.show_error(f"Could not open the GitHub page:\n{exc}")

    def _open_folder(self, *, use_selected_output: bool = False) -> None:
        if use_selected_output:
            source_id = ID_OUTPUT_PAGE if self.active_page == "output" else ID_OUTPUT
            self._commit_output_folder(source_id, notify=True)
        if self._result and not use_selected_output:
            target = self._result.output_directory
        else:
            output_control = ID_OUTPUT_PAGE if self.active_page == "output" else ID_OUTPUT
            target = Path(self._text(output_control)).expanduser()
        if target.exists():
            os.startfile(target)
        else:
            self.show_error(f"The output folder does not exist yet:\n{target}")

    def _text(self, control_id: int) -> str:
        hwnd = self.controls[control_id]
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _checked(self, control_id: int) -> bool:
        return self.check_state[control_id]

    def _enable(self, control_id: int, enabled: bool) -> None:
        hwnd = self.controls.get(control_id)
        if hwnd:
            user32.EnableWindow(hwnd, enabled)
            self._invalidate_control(control_id)

    def _invalidate_control(self, control_id: int) -> None:
        hwnd = self.controls.get(control_id)
        if hwnd:
            user32.InvalidateRect(hwnd, None, True)

    def _smoke_test_log_readonly(self) -> None:
        """Exercise user-edit messages while confirming app-owned log updates still work."""
        hwnd = self.controls.get(ID_LOG)
        if not hwnd:
            raise RuntimeError("Progress log control was not created.")
        marker = "CaptureWebsite log read-only smoke"
        self._set_text(ID_LOG, marker)
        user32.SendMessageW(hwnd, WM_CHAR, ord("X"), 1)
        user32.SendMessageW(hwnd, WM_KEYDOWN, VK_BACK, 0)
        user32.SendMessageW(hwnd, WM_PASTE, 0, 0)
        if self._text(ID_LOG) != marker:
            raise RuntimeError("Progress log accepted user-edit input during the smoke test.")
        self._append_log("programmatic append")
        if self._text(ID_LOG) != marker + "\r\nprogrammatic append":
            raise RuntimeError("Progress log rejected an application-owned append during the smoke test.")
        self._set_text(ID_LOG, "")

    def _set_text(self, control_id: int, text: str) -> None:
        hwnd = self.controls.get(control_id)
        if hwnd:
            user32.SetWindowTextW(hwnd, text)

    def _set_status(self, text: str, *, append: bool = False) -> None:
        self._set_text(ID_STATUS, text)
        if append:
            self._append_log(text)

    def _append_log(self, text: str) -> None:
        hwnd = self.controls.get(ID_LOG)
        if not hwnd:
            return
        existing = self._text(ID_LOG)
        suffix = "\r\n" if existing else ""
        updated = existing + suffix + text
        user32.SetWindowTextW(hwnd, updated)
        end = len(updated)
        user32.SendMessageW(hwnd, EM_SETSEL, end, end)
        user32.SendMessageW(hwnd, EM_SCROLLCARET, 0, 0)

    def show_error(self, text: str) -> None:
        user32.MessageBoxW(self.hwnd, text, PRODUCT_NAME, 0x10)


def main() -> int:
    if "--self-test-capture" in sys.argv[1:]:
        try:
            index = sys.argv.index("--self-test-capture")
            url = sys.argv[index + 1]
            output = Path(sys.argv[index + 2])
            config = CaptureConfig(url=url, output_dir=output, full=True, timeout=45)
            CaptureEngine(config).run(threading.Event())
            return 0
        except Exception:
            return 1
    try:
        return SiteCaptureApp().run(smoke_test="--smoke-test" in sys.argv[1:])
    except Exception as exc:
        if sys.platform == "win32":
            user32.MessageBoxW(None, str(exc), PRODUCT_NAME, 0x10)
            return 1
        print(f"CaptureWebsite GUI error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
