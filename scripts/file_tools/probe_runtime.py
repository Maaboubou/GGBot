#!/usr/bin/env python3
"""Discover Codex and document tools in the runtime that will execute them.

This script deliberately uses only the Python standard library.  The Windows
chatbot invokes it directly through ``wsl.exe python3`` so no shell variables
need to survive the Windows-to-WSL command boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


FILE_TOOL_COMMANDS = (
    "pdftotext",
    "pdfinfo",
    "pdftoppm",
    "qpdf",
    "gs",
    "mutool",
    "tesseract",
    "ocrmypdf",
    "pandoc",
    "markitdown",
    "mammoth",
    "weasyprint",
    "magick",
    "7z",
    "bsdtar",
    "file",
    "exiftool",
    "mediainfo",
    "clamscan",
)


def _unique_paths(values: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        expanded = value.expanduser()
        key = os.path.normcase(str(expanded))
        if key not in seen:
            seen.add(key)
            result.append(expanded)
    return result


def _candidate_path(home: Path) -> str:
    inherited = [Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item]

    def node_version_key(path: Path) -> tuple[int, ...]:
        numbers = re.findall(r"\d+", path.parent.name)
        return tuple(int(number) for number in numbers)

    nvm_bins = sorted(
        (home / ".nvm/versions/node").glob("*/bin"),
        key=node_version_key,
        reverse=True,
    )
    native_inherited = [path for path in inherited if not _is_windows_mount(path)]
    paths = _unique_paths(
        [
            home / ".local/bin",
            *nvm_bins,
            home / ".hermes/node/bin",
            *native_inherited,
            Path("/usr/local/bin"),
            Path("/usr/bin"),
            Path("/bin"),
        ]
    )
    return os.pathsep.join(str(path) for path in paths)


def _resolve_command(name: str, search_path: str) -> Optional[Path]:
    candidate = Path(name).expanduser()
    if candidate.is_absolute() or os.sep in name:
        return candidate.absolute() if candidate.is_file() and os.access(candidate, os.X_OK) else None
    resolved = shutil.which(name, path=search_path)
    return Path(resolved).absolute() if resolved else None


def _is_windows_mount(path: Path) -> bool:
    value = path.expanduser().absolute().as_posix()
    return bool(re.match(r"^/mnt/[a-z](?:/|$)", value, flags=re.IGNORECASE))


def _native_executable(path: Optional[Path]) -> Optional[Path]:
    """Reject Windows-mounted launchers even when reached through a symlink."""
    if path is None or _is_windows_mount(path):
        return None
    try:
        realpath = path.resolve(strict=True)
    except OSError:
        return None
    if _is_windows_mount(realpath) or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return path.absolute()


def _codex_candidates(search_path: str, selected: Optional[Path], home: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    if selected:
        candidates.append(selected)
    for directory_value in search_path.split(os.pathsep):
        directory = Path(directory_value)
        if _is_windows_mount(directory) or not directory.is_dir():
            continue
        try:
            if _is_relative_to(directory.resolve(), home / ".codex/tmp"):
                continue
        except OSError:
            continue
        try:
            entries = sorted(
                entry
                for entry in directory.glob("codex*")
                if entry.name not in {"codex-execve-wrapper", "codex-linux-sandbox"}
            )
        except OSError:
            continue
        candidates.extend(entries[:20])

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    selected_value = str(selected) if selected else ""
    for candidate in candidates:
        executable = _native_executable(candidate)
        if executable is None:
            continue
        key = str(executable)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "path": key,
                "realpath": str(executable.resolve()),
                "selected": key == selected_value,
            }
        )
    return result


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_user_root(path: Path, home: Path, uid: Optional[int]) -> bool:
    """Accept only existing roots protected by the runtime user's home."""
    try:
        normalized = path.expanduser().resolve(strict=True)
        normalized_home = home.resolve(strict=True)
    except OSError:
        return False
    if not _is_relative_to(normalized, normalized_home) or normalized == normalized_home:
        return False

    try:
        home_metadata = normalized_home.stat()
    except OSError:
        return False
    if uid is not None and home_metadata.st_uid != uid:
        return False
    if stat.S_IMODE(home_metadata.st_mode) & 0o022:
        return False

    current = normalized
    while current != normalized_home:
        try:
            metadata = current.stat()
        except OSError:
            return False
        if uid is not None and metadata.st_uid != uid:
            return False
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            return False
        current = current.parent
    return True


def _tool_root(path: Path, realpath: Path, home: Path) -> Optional[Path]:
    local_share = home / ".local/share"
    uv_tools = local_share / "uv/tools"
    for candidate in (realpath, path):
        if _is_relative_to(candidate, uv_tools):
            relative = candidate.relative_to(uv_tools)
            return uv_tools / relative.parts[0] if relative.parts else None
        if _is_relative_to(candidate, local_share):
            relative = candidate.relative_to(local_share)
            if len(relative.parts) >= 3 and relative.parts[1] in {"bin", "Scripts"}:
                return local_share / relative.parts[0]
    return None


def _codex_root(path: Path, realpath: Path, home: Path) -> Optional[Path]:
    nvm_root = home / ".nvm/versions/node"
    for candidate in (path, realpath):
        if _is_relative_to(candidate, nvm_root):
            relative = candidate.relative_to(nvm_root)
            return nvm_root / relative.parts[0] if relative.parts else None
    hermes_root = home / ".hermes/node"
    if _is_relative_to(path, hermes_root) or _is_relative_to(realpath, hermes_root):
        return hermes_root
    try:
        common = Path(os.path.commonpath((str(path), str(realpath))))
    except ValueError:
        return None
    return common if _is_relative_to(common, home) and common != home else None


def _configured_roots(values: Iterable[str], home: Path, uid: Optional[int]) -> list[Path]:
    roots: list[Path] = []
    for value in values:
        if not value:
            continue
        path = Path(value).expanduser()
        if _safe_user_root(path, home, uid):
            roots.append(path.resolve())
    return roots


def _registered_roots(home: Path, uid: Optional[int]) -> list[str]:
    manifest = home / ".local/share/wxautox/runtime/file-tools.json"
    try:
        metadata = manifest.stat()
        if uid is not None and metadata.st_uid != uid:
            return []
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            return []
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    values = payload.get("registered_roots") or payload.get("tool_roots") or []
    return [str(value) for value in values if isinstance(value, str)]


def probe(
    *,
    codex_bin: str = "codex",
    configured_roots: Iterable[str] = (),
) -> dict[str, Any]:
    home = Path.home().resolve()
    getuid = getattr(os, "getuid", None)
    uid = int(getuid()) if callable(getuid) else None
    search_path = _candidate_path(home)

    codex_path = _native_executable(_resolve_command(codex_bin or "codex", search_path))
    codex: dict[str, Any] = {"configured": codex_bin or "codex", "available": bool(codex_path)}
    permission_roots: list[Path] = []
    path_dirs: list[Path] = [home / ".local/bin"]
    if codex_path:
        codex_realpath = codex_path.resolve()
        codex.update({"path": str(codex_path), "realpath": str(codex_realpath)})
        path_dirs.append(codex_path.parent)
        root = _codex_root(codex_path, codex_realpath, home)
        if root and _safe_user_root(root, home, uid):
            permission_roots.append(root.resolve())

    commands: list[dict[str, str]] = []
    tool_roots: list[Path] = []
    for name in FILE_TOOL_COMMANDS:
        command_path = _resolve_command(name, search_path)
        if not command_path:
            continue
        realpath = command_path.resolve()
        command: dict[str, str] = {
            "name": name,
            "path": str(command_path),
            "realpath": str(realpath),
        }
        path_dirs.append(command_path.parent)
        root = _tool_root(command_path, realpath, home)
        if root and _safe_user_root(root, home, uid):
            resolved_root = root.resolve()
            command["root"] = str(resolved_root)
            tool_roots.append(resolved_root)
            permission_roots.append(resolved_root)
        commands.append(command)

    local_bin = home / ".local/bin"
    if local_bin.is_dir() and _safe_user_root(local_bin, home, uid):
        permission_roots.append(local_bin.resolve())
    registered_roots = _configured_roots(
        [*configured_roots, *_registered_roots(home, uid)],
        home,
        uid,
    )
    tool_roots.extend(registered_roots)
    permission_roots.extend(registered_roots)

    safe_permission_roots = _unique_paths(permission_roots)
    safe_tool_roots = _unique_paths(tool_roots)
    safe_path_dirs = [
        path.resolve()
        for path in _unique_paths(path_dirs)
        if path.is_dir() and (_is_relative_to(path.resolve(), home) or str(path).startswith("/usr/"))
    ]
    now = datetime.now(timezone.utc).isoformat()
    status = "ready" if codex_path else "unavailable"
    return {
        "status": status,
        "runtime": "wsl" if os.environ.get("WSL_DISTRO_NAME") else ("windows" if os.name == "nt" else "linux"),
        "home": str(home),
        "uid": uid,
        "codex": codex,
        "codex_candidates": _codex_candidates(search_path, codex_path, home),
        "commands": commands,
        "command_names": [item["name"] for item in commands],
        "tool_roots": [str(path) for path in safe_tool_roots],
        "registered_roots": [str(path) for path in _unique_paths(registered_roots)],
        "permission_roots": [str(path) for path in safe_permission_roots],
        "path_dirs": [str(path) for path in safe_path_dirs],
        "probed_at": now,
    }


def _write_manifest(snapshot: dict[str, Any], target: Path) -> None:
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".file-tools-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_PROXY_WSL_BIN", "codex"))
    parser.add_argument("--trusted-root", action="append", default=[])
    parser.add_argument("--write-manifest", nargs="?", const="~/.local/share/wxautox/runtime/file-tools.json")
    parser.add_argument("--json", action="store_true", help="Retained for an explicit machine-readable CLI contract.")
    args = parser.parse_args()
    snapshot = probe(codex_bin=args.codex_bin, configured_roots=args.trusted_root)
    if args.write_manifest:
        manifest = Path(args.write_manifest).expanduser()
        _write_manifest(snapshot, manifest)
        snapshot["manifest_path"] = str(manifest)
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    return 0 if snapshot["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
