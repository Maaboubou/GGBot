#!/usr/bin/env python3
"""Launch Codex in WSL without an inline shell command."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
from pathlib import Path

from probe_runtime import probe


def _first_existing(roots: list[Path], relative: str, *, file: bool = False) -> str:
    for root in roots:
        candidate = root / relative
        exists = candidate.is_file() if file else candidate.is_dir()
        if exists:
            return str(candidate)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--path-dir", action="append", default=[])
    parser.add_argument("--tool-root", action="append", default=[])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()

    if options.payload:
        try:
            payload = json.loads(base64.urlsafe_b64decode(options.payload.encode("ascii")))
        except (binascii.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid Codex runtime payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("Invalid Codex runtime payload: object required")
        options.codex_bin = str(payload.get("codex_bin") or "codex")
        options.path_dir = [str(value) for value in payload.get("path_dirs") or []]
        options.tool_root = [str(value) for value in payload.get("tool_roots") or []]
        options.args = [str(value) for value in payload.get("args") or []]

    runtime = probe(codex_bin=options.codex_bin, configured_roots=options.tool_root)
    codex_path = str(runtime.get("codex", {}).get("path") or options.codex_bin)
    roots = [Path(item) for item in options.tool_root or runtime.get("tool_roots", [])]
    path_dirs = [Path(item) for item in options.path_dir or runtime.get("path_dirs", [])]
    path_dirs.insert(0, Path(codex_path).parent)
    path_dirs.append(Path.home() / ".local/bin")

    environment = dict(os.environ)
    existing_path = environment.get("PATH", "")
    unique_dirs = list(dict.fromkeys(str(path) for path in path_dirs if path.is_dir()))
    environment["PATH"] = os.pathsep.join([*unique_dirs, existing_path])

    settings = {
        "TESSDATA_PREFIX": _first_existing(roots, "share/tessdata"),
        "MAGICK_CONFIGURE_PATH": _first_existing(roots, "etc/ImageMagick-7"),
        "FONTCONFIG_FILE": _first_existing(roots, "etc/fonts/mabobot-fonts.conf", file=True),
        "MAGIC": _first_existing(roots, "share/misc/magic.mgc", file=True),
    }
    for name, value in settings.items():
        if value and not environment.get(name):
            environment[name] = value
    library_dirs = [str(root / "lib") for root in roots if (root / "lib").is_dir()]
    if library_dirs:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            [*library_dirs, environment.get("LD_LIBRARY_PATH", "")]
        ).rstrip(os.pathsep)

    executable = codex_path if Path(codex_path).is_file() else shutil.which(codex_path, path=environment["PATH"])
    if not executable:
        raise SystemExit(f"Codex CLI unavailable: {options.codex_bin}")
    arguments = list(options.args)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    os.execvpe(executable, [executable, *arguments], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
