"""mabowx 部署环境自检。

用法：

    python -m mabowx.env_check
    python -c "from mabowx.env_check import check_env; check_env()"
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
from pathlib import Path

_REQUIRED = [
    ("comtypes", "comtypes"),
    ("PIL", "Pillow"),
    ("pyperclip", "pyperclip"),
    ("psutil", "psutil"),
    ("tenacity", "tenacity"),
    ("yaml", "PyYAML"),
    ("win32api", "pywin32"),
    ("win32con", "pywin32"),
    ("win32event", "pywin32"),
    ("win32gui", "pywin32"),
    ("win32process", "pywin32"),
    ("win32security", "pywin32"),
    ("win32ts", "pywin32"),
    ("win32ui", "pywin32"),
]

_OPTIONAL = [
    ("colorama", "colorama"),
]


def _module_name(module: str) -> str:
    return module.split(".", 1)[0]


def check_env() -> dict:
    """检查依赖并返回结构化结果；不抛异常。"""
    result = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "os": os.name,
        "mabowx_file": str(Path(__file__).resolve()),
        "missing": [],
        "installed": [],
        "uia_backend": None,
        "ok": True,
    }

    for module, pip_name in _REQUIRED:
        try:
            importlib.import_module(module)
            result["installed"].append(pip_name)
        except Exception as exc:
            result["missing"].append({"module": module, "pip_name": pip_name, "error": str(exc)})
            result["ok"] = False

    for module, pip_name in _OPTIONAL:
        try:
            importlib.import_module(module)
            result["installed"].append(pip_name)
        except Exception:
            pass

    try:
        from mabowx.core import uia

        if uia.auto is None:
            result["uia_backend"] = "unavailable (not on Windows)"
        else:
            backend = getattr(uia.auto, "__name__", str(uia.auto))
            result["uia_backend"] = "vendored" if "vendor" in backend else "system"
    except Exception as exc:
        result["uia_backend"] = f"unavailable: {exc}"
        result["ok"] = False

    return result


def print_report() -> None:
    info = check_env()
    print("=== mabowx environment check ===")
    print(f"Python : {info['python']}")
    print(f"Exec   : {info['executable']}")
    print(f"Package: {info['mabowx_file']}")
    print(f"UIA    : {info['uia_backend']}")
    if info["missing"]:
        print("MISSING:")
        for item in info["missing"]:
            print(f"  - {item['module']}  ->  python -m pip install {item['pip_name']}")
    else:
        print("Required dependencies: OK")
    print("=================================")


if __name__ == "__main__":
    print_report()
