#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
开机后自动处理微信登录确认，并在确认在线后启动 wxautox。

流程：
1. 等待 Windows / 微信自动启动稳定。
2. 查找微信登录确认窗口，置前后发送 Enter。
3. 用 wxautox4 的 WeChat().IsOnline() 校验是否真正在线。
4. 在线后启动 Start-GUI.bat；超时仍未在线则发送邮件通知，通常是二维码登录场景。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
LOG_FILE = LOG_DIR / "wechat_auto_login.log"

WECHAT_TITLES = {"微信", "WeChat"}
VK_RETURN = 0x0D
SW_RESTORE = 9


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def score(self) -> int:
        score = 0
        if "login" in self.class_name.lower():
            score += 100
        if self.title in WECHAT_TITLES:
            score += 20
        if 260 <= self.width <= 620 and 320 <= self.height <= 760:
            score += 50
        return score


@dataclass
class DisplayInfo:
    primary_width: int
    primary_height: int
    virtual_width: int
    virtual_height: int
    session_name: str

    @property
    def ready(self) -> bool:
        return self.primary_width >= 1280 and self.primary_height >= 720


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def ensure_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("wechat_auto_login.py 必须在 Windows Python 中运行")


def load_project_env() -> None:
    load_dotenv(ROOT_DIR / ".env")
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))


def get_display_info() -> DisplayInfo:
    ensure_windows()
    user32 = ctypes.windll.user32
    return DisplayInfo(
        primary_width=int(user32.GetSystemMetrics(0)),
        primary_height=int(user32.GetSystemMetrics(1)),
        virtual_width=int(user32.GetSystemMetrics(78)),
        virtual_height=int(user32.GetSystemMetrics(79)),
        session_name=os.environ.get("SESSIONNAME", ""),
    )


def wait_for_display_ready(timeout: int, poll_interval: float) -> bool:
    deadline = time.monotonic() + max(0, timeout)
    last_info: DisplayInfo | None = None

    while True:
        info = get_display_info()
        if (
            last_info is None
            or info.primary_width != last_info.primary_width
            or info.primary_height != last_info.primary_height
            or info.virtual_width != last_info.virtual_width
            or info.virtual_height != last_info.virtual_height
        ):
            logging.info(
                "当前显示环境: primary=%sx%s virtual=%sx%s session=%s",
                info.primary_width,
                info.primary_height,
                info.virtual_width,
                info.virtual_height,
                info.session_name,
            )
            last_info = info

        if info.ready:
            return True

        if time.monotonic() >= deadline:
            logging.warning(
                "显示环境未达标，当前 primary=%sx%s，要求至少 1280x720",
                info.primary_width,
                info.primary_height,
            )
            return False

        time.sleep(max(1.0, poll_interval))


def iter_windows() -> Iterable[WindowInfo]:
    ensure_windows()
    user32 = ctypes.windll.user32

    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p]
    enum_windows.restype = ctypes.c_bool

    is_window_visible = user32.IsWindowVisible
    get_window_text_length = user32.GetWindowTextLengthW
    get_window_text = user32.GetWindowTextW
    get_class_name = user32.GetClassNameW
    get_window_rect = user32.GetWindowRect

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    windows: list[WindowInfo] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not is_window_visible(hwnd):
            return True

        title_len = get_window_text_length(hwnd)
        if title_len <= 0:
            return True

        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        get_window_text(hwnd, title_buf, title_len + 1)
        title = title_buf.value.strip()
        if title not in WECHAT_TITLES:
            return True

        class_buf = ctypes.create_unicode_buffer(256)
        get_class_name(hwnd, class_buf, 256)

        rect = RECT()
        if not get_window_rect(hwnd, ctypes.byref(rect)):
            return True

        info = WindowInfo(
            hwnd=int(hwnd),
            title=title,
            class_name=class_buf.value.strip(),
            left=rect.left,
            top=rect.top,
            right=rect.right,
            bottom=rect.bottom,
        )
        windows.append(info)
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
    enum_windows(enum_proc, 0)
    return windows


def find_login_window() -> WindowInfo | None:
    candidates = [
        window
        for window in iter_windows()
        if "login" in window.class_name.lower()
        or (260 <= window.width <= 620 and 320 <= window.height <= 760)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def find_main_window() -> WindowInfo | None:
    candidates = [
        window
        for window in iter_windows()
        if "mainwindow" in window.class_name.lower()
        or (window.width >= 420 and window.height >= 360)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]


def focus_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.2)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)


def press_enter() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, 2, 0)


def press_enter_on_login_window(window: WindowInfo) -> None:
    logging.info(
        "发现微信登录窗口: hwnd=%s title=%s class=%s size=%sx%s",
        window.hwnd,
        window.title,
        window.class_name,
        window.width,
        window.height,
    )
    focus_window(window.hwnd)
    press_enter()
    logging.info("已向微信登录窗口发送 Enter")


def log_display_info() -> None:
    info = get_display_info()
    logging.info(
        "当前显示环境: primary=%sx%s virtual=%sx%s session=%s",
        info.primary_width,
        info.primary_height,
        info.virtual_width,
        info.virtual_height,
        info.session_name,
    )


def wxautox_already_running() -> bool:
    try:
        import requests

        bridge_port = (os.getenv("WX_BOT_PORT") or "5000").strip()
        response = requests.get(f"http://127.0.0.1:{bridge_port}/health", timeout=2)
        if response.ok:
            data = response.json()
            return bool(data.get("wechat_connected") and data.get("wechat_online"))
    except Exception:
        return False
    return False


def python_executable_for_project() -> str:
    candidates = [
        ROOT_DIR / ".venv" / "Scripts" / "python.exe",
        ROOT_DIR / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def start_wxautox(start_target: str) -> None:
    if wxautox_already_running():
        logging.info("wxautox 已经在线，跳过重复启动")
        return

    target = (ROOT_DIR / start_target).resolve()
    if not target.exists():
        raise FileNotFoundError(f"wxautox 启动目标不存在: {target}")

    if target.suffix.lower() == ".bat":
        command = ["cmd.exe", "/c", str(target)]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    elif target.suffix.lower() == ".py":
        command = [python_executable_for_project(), str(target)]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        command = [str(target)]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    subprocess.Popen(
        command,
        cwd=str(ROOT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    logging.info("已启动 wxautox: %s", target)


def send_qr_required_email(reason: str) -> bool:
    from app.services.email_service import send_alert_email

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = "🚨 微信开机自动登录失败，需要扫码确认"
    body = (
        "微信开机自动登录未成功，wxautox 未启动。\n\n"
        f"时间：{now}\n"
        f"主机：{os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'unknown'}\n"
        f"原因：{reason}\n\n"
        "常见情况：微信显示二维码登录界面，按 Enter 无法进入微信。\n"
        "请远程到这台电脑扫码登录微信，登录完成后再启动 wxautox。"
    )
    return send_alert_email(subject=subject, body=body)


def wait_and_login(args: argparse.Namespace) -> bool:
    deadline = time.monotonic() + args.login_timeout
    next_enter_at = 0.0
    enter_sent = False

    while time.monotonic() < deadline:
        if wxautox_already_running():
            return True

        main_window = find_main_window()
        if main_window:
            logging.info(
                "发现微信主窗口，认为登录已完成: hwnd=%s title=%s class=%s size=%sx%s",
                main_window.hwnd,
                main_window.title,
                main_window.class_name,
                main_window.width,
                main_window.height,
            )
            return True

        window = find_login_window()
        if window and time.monotonic() >= next_enter_at:
            press_enter_on_login_window(window)
            enter_sent = True
            next_enter_at = time.monotonic() + args.enter_interval

        time.sleep(args.poll_interval)

    if enter_sent:
        logging.warning("已尝试 Enter，但微信仍未在线，判断为二维码/手动登录场景")
    else:
        logging.warning("超时内没有找到可处理的微信登录确认窗口")
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动确认微信登录并启动 wxautox")
    parser.add_argument("--initial-delay", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_INITIAL_DELAY", "60")))
    parser.add_argument("--login-timeout", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_TIMEOUT", "180")))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("WECHAT_AUTOLOGIN_POLL_INTERVAL", "5")))
    parser.add_argument("--enter-interval", type=float, default=float(os.getenv("WECHAT_AUTOLOGIN_ENTER_INTERVAL", "20")))
    parser.add_argument("--display-timeout", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_DISPLAY_TIMEOUT", "180")))
    parser.add_argument("--start-delay", type=int, default=int(os.getenv("WECHAT_AUTOLOGIN_START_DELAY", "15")))
    parser.add_argument("--start-target", default=os.getenv("WXAUTOX_START_TARGET", "Start-GUI.bat"))
    parser.add_argument("--no-email", action="store_true", help="失败时不发邮件，仅写日志")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    load_project_env()

    try:
        ensure_windows()
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 2

    args = parse_args()
    logging.info("微信自动登录启动，初始等待 %ss", args.initial_delay)
    log_display_info()
    time.sleep(max(0, args.initial_delay))

    display_ready = wait_for_display_ready(args.display_timeout, args.poll_interval)

    if wait_and_login(args):
        if not display_ready:
            if not args.no_email:
                send_qr_required_email(
                    reason=(
                        "微信已可进入，但显示环境低于 1280x720。"
                        "为避免 wxautox 在 640x480/headless 桌面下错误调整窗口，本次未启动。"
                    )
                )
            return 1
        if args.start_delay > 0:
            logging.info("微信在线后等待 %ss 再启动 wxautox", args.start_delay)
            time.sleep(args.start_delay)
        start_wxautox(args.start_target)
        return 0

    if not args.no_email:
        send_qr_required_email(
            reason=(
                f"{args.login_timeout}s 内未通过 wxautox4 IsOnline() 校验。"
                "如果屏幕上是二维码登录界面，这是预期告警。"
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
