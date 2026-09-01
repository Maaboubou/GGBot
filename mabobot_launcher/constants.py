"""Shared paths and service definitions for the desktop launcher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
UI_DIR = Path(__file__).resolve().parent / "ui"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_FILE = ASSET_DIR / "mabobot.ico"
APP_ICON_IMAGE = ASSET_DIR / "mabobot-icon.png"

SETTINGS_FILE = DATA_DIR / "launcher_settings.json"
STATE_FILE = DATA_DIR / "launcher_state.json"
CONTROL_SIGNAL_FILE = PROJECT_ROOT / ".restart_signal"
SHOW_SIGNAL_FILE = PROJECT_ROOT / ".launcher_show"
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "scripts" / "launcher" / "bootstrap.ps1"
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install.ps1"

LAUNCHER_ID = "mabobot.desktop.launcher"
SIGNAL_PROTOCOL = 4


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    script: str
    port_env: str
    default_port: int
    health_path: str = "/health"
    startup_grace_seconds: float = 45.0


SERVICES = (
    ServiceSpec("bot", "微信 Bot", "wx_bot.py", "WX_BOT_PORT", 5555),
    ServiceSpec(
        "web",
        "Web 服务",
        "start.py",
        "WEB_PORT",
        8888,
        startup_grace_seconds=150.0,
    ),
)
