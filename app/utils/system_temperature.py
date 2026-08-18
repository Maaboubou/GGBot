"""Best-effort hardware temperature discovery for the system dashboard.

Temperature support varies considerably by operating system. Linux commonly
exposes sensors through psutil, while Windows needs a hardware monitor that
publishes its sensors through WMI. All sources are optional so a missing
sensor can never break the main system-status response.
"""

import copy
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil


_CACHE_SECONDS = 20.0
_cache_lock = threading.Lock()
_cache_time = 0.0
_cache_value: Optional[Dict[str, Any]] = None


def _is_windows() -> bool:
    return os.name == "nt"


def _valid_celsius(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Keep legitimate sub-zero ambient readings while rejecting values that
    # are outside the range of consumer hardware sensors.
    if not math.isfinite(number) or number <= -40 or number > 150:
        return None
    return round(number, 1)


def _sensor_type(name: str, group: str = "", parent: str = "") -> str:
    sensor_path = parent.casefold()
    if any(token in sensor_path for token in ("/amdcpu/", "/intelcpu/", "/cpu/")):
        return "cpu"
    if any(token in sensor_path for token in ("/gpu/", "/atigpu/", "/nvidiagpu/")):
        return "gpu"
    if any(token in sensor_path for token in ("/ssd/", "/hdd/", "/nvme/")):
        return "storage"
    if "/lpc/" in sensor_path:
        return "system"

    haystack = " ".join((name, group, parent)).casefold()
    if any(token in haystack for token in (
        "coretemp", "k10temp", "zenpower", "amdcpu", "intelcpu", "cpu", "tctl", "tdie",
    )):
        return "cpu"
    if any(token in haystack for token in ("amdgpu", "nvidia", "gpu", "graphics")):
        return "gpu"
    if any(token in haystack for token in ("nvme", "ssd", "hdd", "drive", "storage")):
        return "storage"
    if any(token in haystack for token in (
        "acpi", "thermal zone", "motherboard", "mainboard", "chipset", "pch", "system",
    )):
        return "system"
    return "other"


def _reading(
    name: str,
    value: Any,
    source: str,
    *,
    group: str = "",
    parent: str = "",
    sensor_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    celsius = _valid_celsius(value)
    if celsius is None:
        return None
    clean_name = str(name or group or "温度").strip()[:80]
    return {
        "name": clean_name,
        "type": sensor_type or _sensor_type(clean_name, group, parent),
        "celsius": celsius,
        "source": source,
    }


def _read_psutil_temperatures() -> List[Dict[str, Any]]:
    reader = getattr(psutil, "sensors_temperatures", None)
    if not callable(reader):
        return []
    try:
        groups = reader(fahrenheit=False) or {}
    except (AttributeError, NotImplementedError, OSError, RuntimeError):
        return []

    readings: List[Dict[str, Any]] = []
    for group, entries in groups.items():
        for entry in entries or []:
            label = getattr(entry, "label", "") or str(group)
            item = _reading(label, getattr(entry, "current", None), "psutil", group=str(group))
            if item:
                readings.append(item)
    return readings


def _powershell_executable() -> Optional[str]:
    return shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("powershell")


def _temperature_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return _valid_celsius(value)
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value or ""))
    if not match:
        return None
    return _valid_celsius(match.group(0).replace(",", "."))


def _parse_lhm_http_payload(payload: Any) -> List[Dict[str, Any]]:
    readings: List[Dict[str, Any]] = []

    def visit(node: Any, ancestors: List[str]) -> None:
        if not isinstance(node, dict):
            return
        name = str(node.get("Text") or node.get("Name") or "").strip()
        sensor_id = str(node.get("SensorId") or "")
        sensor_kind = str(node.get("Type") or "").casefold()
        is_temperature = sensor_kind == "temperature" or "/temperature/" in sensor_id.casefold()
        if is_temperature:
            value = _temperature_number(node.get("RawValue"))
            if value is None:
                value = _temperature_number(node.get("Value"))
            item = _reading(
                name or "温度",
                value,
                "LibreHardwareMonitor HTTP",
                parent=" ".join(ancestors + [sensor_id]),
            )
            if item:
                readings.append(item)
        next_ancestors = ancestors + ([name] if name else [])
        for child in node.get("Children") or []:
            visit(child, next_ancestors)

    visit(payload, [])
    return readings


def _read_lhm_http_temperatures() -> List[Dict[str, Any]]:
    try:
        addresses = ["127.0.0.1"]
        for info in socket.getaddrinfo(socket.gethostname(), 8085, socket.AF_INET):
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
    except OSError:
        addresses = ["127.0.0.1"]

    # LHM 0.9.6 offers either all interfaces or one concrete local interface,
    # but not a dedicated loopback entry. Try both safe, machine-local forms.
    for address in addresses:
        request = Request(
            f"http://{address}:8085/data.json",
            headers={"User-Agent": "wxautox4-system-monitor/1.0"},
        )
        try:
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
        except (OSError, HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
            continue
        readings = _parse_lhm_http_payload(payload)
        if readings:
            return readings
    return []


def _read_windows_monitor_temperatures() -> List[Dict[str, Any]]:
    executable = _powershell_executable()
    if not executable:
        return []

    # Both monitor applications expose the same Sensor WMI shape. Query both
    # namespaces in one process and ignore whichever one is not running.
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$items = @()
foreach ($monitor in @(
    @{ Namespace = 'root/LibreHardwareMonitor'; Source = 'LibreHardwareMonitor' },
    @{ Namespace = 'root/OpenHardwareMonitor'; Source = 'OpenHardwareMonitor' }
)) {
    try {
        $items += @(Get-CimInstance -Namespace $monitor.Namespace -ClassName Sensor |
            Where-Object { $_.SensorType -eq 'Temperature' } |
            ForEach-Object {
                [PSCustomObject]@{
                    name = [string]$_.Name
                    value = [double]$_.Value
                    parent = [string]$_.Parent
                    source = $monitor.Source
                }
            })
    } catch {}
}
@($items) | ConvertTo-Json -Compress
""".strip()
    kwargs: Dict[str, Any] = {
        "args": [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        "capture_output": True,
        "text": True,
        "encoding": "utf-8-sig",
        "errors": "replace",
        "timeout": 5,
        "check": False,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(**kwargs)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    if isinstance(payload, dict):
        payload = [payload]
    readings: List[Dict[str, Any]] = []
    for sensor in payload if isinstance(payload, list) else []:
        if not isinstance(sensor, dict):
            continue
        item = _reading(
            sensor.get("name", "温度"),
            sensor.get("value"),
            str(sensor.get("source") or "HardwareMonitor"),
            parent=str(sensor.get("parent") or ""),
        )
        if item:
            readings.append(item)
    return readings


def _read_nvidia_temperatures() -> List[Dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    kwargs: Dict[str, Any] = {
        "args": [
            executable,
            "--query-gpu=name,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 3,
        "check": False,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(**kwargs)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    readings: List[Dict[str, Any]] = []
    for index, line in enumerate(result.stdout.splitlines()):
        if "," not in line:
            continue
        name, value = line.rsplit(",", 1)
        item = _reading(
            f"GPU {index + 1} · {name.strip()}",
            value.strip(),
            "nvidia-smi",
            sensor_type="gpu",
        )
        if item:
            readings.append(item)
    return readings


def _summarize(readings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    labels = {
        "cpu": "CPU",
        "gpu": "GPU",
        "storage": "存储",
        "system": "主板",
        "other": "其他",
    }
    summary: List[Dict[str, Any]] = []
    for sensor_type in ("cpu", "gpu", "storage", "system", "other"):
        candidates = [item for item in readings if item["type"] == sensor_type]
        if not candidates:
            continue
        hottest = max(candidates, key=lambda item: item["celsius"])
        summary.append({
            "label": labels[sensor_type],
            "type": sensor_type,
            "celsius": hottest["celsius"],
            "source": hottest["source"],
            "sensor_name": hottest["name"],
        })
    return summary


def collect_temperature_status() -> Dict[str, Any]:
    readings = _read_psutil_temperatures()
    if _is_windows():
        readings.extend(_read_lhm_http_temperatures())
        readings.extend(_read_windows_monitor_temperatures())
    readings.extend(_read_nvidia_temperatures())

    # Multiple sources can report the same sensor. Keep the first source while
    # making the response stable and compact for the dashboard.
    unique: List[Dict[str, Any]] = []
    seen = set()
    for item in readings:
        key = (item["type"], item["name"].casefold(), item["celsius"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    sensors = _summarize(unique)
    sources = list(dict.fromkeys(item["source"] for item in unique))
    if sensors:
        message = None
    elif _is_windows():
        message = "未检测到温度传感器；请运行 LibreHardwareMonitor 并开启 Remote Web Server"
    else:
        message = "当前系统未暴露可读取的温度传感器"
    return {
        "available": bool(sensors),
        "sensors": sensors,
        "sources": sources,
        "message": message,
    }


def get_temperature_status(force_refresh: bool = False) -> Dict[str, Any]:
    """Return cached temperature status; discovery may launch a monitor CLI."""
    global _cache_time, _cache_value
    now = time.monotonic()
    with _cache_lock:
        if (
            not force_refresh
            and _cache_value is not None
            and now - _cache_time < _CACHE_SECONDS
        ):
            return copy.deepcopy(_cache_value)
        try:
            value = collect_temperature_status()
        except Exception:
            # Supplemental telemetry must not take down /api/system/status.
            value = {
                "available": False,
                "sensors": [],
                "sources": [],
                "message": "温度传感器读取失败",
            }
        _cache_value = value
        _cache_time = now
        return copy.deepcopy(value)
