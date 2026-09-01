#!/usr/bin/env python3
"""Manage Profile-local Codex Skills inside the target Linux home.

This helper intentionally uses only the Python standard library.  The web
process invokes it inside WSL and exchanges one JSON object over stdin/stdout;
skill content is never interpolated into a shell command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - production helper runs inside Linux/WSL.
    fcntl = None  # type: ignore[assignment]


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
SKILL_NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
GITHUB_OWNER_RE = re.compile(r"^(?=.{1,39}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
GITHUB_REPOSITORY_RE = re.compile(r"^(?=.{1,100}$)[A-Za-z0-9_.-]+$")
GITHUB_REF_RE = re.compile(r"^(?=.{1,128}$)[A-Za-z0-9][A-Za-z0-9._/-]*$")
TRASH_ID_RE = re.compile(
    r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}$"
)
MAX_SKILL_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_GITHUB_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_GITHUB_PACKAGE_FILES = 25_000
MAX_GITHUB_FILE_BYTES = 64 * 1024 * 1024
GITHUB_INSTALL_TIMEOUT_SECONDS = 300
STATE_NAME = ".mabobot-skill-manager.json"
LOCK_NAME = ".mabobot-skill-manager.lock"
TRASH_NAME = ".mabobot-skill-trash"
HISTORY_NAME = ".mabobot-skill-history"
AUDIT_NAME = ".mabobot-skill-audit.jsonl"
STAGING_NAME = ".mabobot-skill-staging"
ORIGIN_NAME = ".mabobot-origin.json"
CONFIG_BEGIN = "# BEGIN mabowx managed Skill state"
CONFIG_END = "# END mabowx managed Skill state"


class SkillError(ValueError):
    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _required(payload: dict[str, Any], key: str, label: str, limit: int) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise SkillError(f"{label}不能为空")
    if len(value) > limit:
        raise SkillError(f"{label}不能超过 {limit} 个字符")
    return value


def _validate_skill_name(value: Any) -> str:
    name = str(value or "").strip()
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillError("Skill 名称只能使用小写字母、数字和单个连字符，不能以连字符开头或结尾，且最长 64 位")
    return name


def _context(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    profile_name = _required(payload, "profile_name", "Profile 名称", 48)
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise SkillError("Profile 名称格式不正确")
    resolved_home = home.expanduser().resolve()
    expected = resolved_home / ".codex" / "mabobot-profiles" / profile_name
    supplied = str(payload.get("codex_home") or "").strip()
    if supplied:
        candidate = Path(os.path.abspath(os.path.normpath(supplied)))
        if candidate != expected:
            raise SkillError("Profile 的 CODEX_HOME 与受管目录不匹配", "unsafe")
    if expected.is_symlink() or not expected.is_dir():
        raise SkillError("Codex Profile 不存在或目录不安全", "not_found")
    try:
        if expected.resolve(strict=True) != expected:
            raise SkillError("Codex Profile 目录链包含符号链接", "unsafe")
    except OSError as exc:
        raise SkillError("无法验证 Codex Profile 目录", "unsafe") from exc
    skills = expected / "skills"
    if skills.is_symlink():
        raise SkillError("Profile Skill 根目录不安全", "unsafe")
    return {
        "home": resolved_home,
        "profile_name": profile_name,
        "codex_home": expected,
        "skills": skills,
        "config": expected / "config.toml",
        "state": expected / STATE_NAME,
        "lock": expected / LOCK_NAME,
        "trash": expected / TRASH_NAME,
        "history": expected / HISTORY_NAME,
        "audit": expected / AUDIT_NAME,
        "staging": expected / STAGING_NAME,
    }


@contextmanager
def _mutation_lock(context: dict[str, Any]) -> Iterator[None]:
    path: Path = context["lock"]
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.chmod(path, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_limited(path: Path, limit: int, *, label: str) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SkillError(f"{label}不存在", "not_found") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise SkillError(f"{label}不是安全的普通文件", "unsafe")
    if info.st_size > limit:
        raise SkillError(f"{label}超过大小限制")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise SkillError(f"无法读取{label}：{exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > limit:
        raise SkillError(f"{label}超过大小限制")
    changed = after.st_size != info.st_size
    if os.name != "nt":
        changed = changed or after.st_ino != info.st_ino or len(content) != info.st_size
    if changed:
        raise SkillError(f"读取{label}时文件发生变化，请重试", "conflict")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{label}必须使用 UTF-8 编码") from exc


def _frontmatter(content: str, *, strict: bool) -> dict[str, str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        if strict:
            raise SkillError("SKILL.md 必须以 YAML frontmatter（---）开头")
        return {}
    end = normalized.find("\n---\n", 4)
    if end < 0:
        if strict:
            raise SkillError("SKILL.md 的 YAML frontmatter 未正确结束")
        return {}
    values: dict[str, str] = {}
    lines = normalized[4:end].splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw in {"|", ">", "|-", ">-", "|+", ">+"}:
            chunks: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith(" ") or not lines[index].strip()):
                chunks.append(lines[index].strip())
                index += 1
            values[key] = " ".join(item for item in chunks if item)
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            try:
                raw = str(json.loads(raw))
            except (ValueError, TypeError):
                pass
        elif len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1].replace("''", "'")
        values[key] = raw
        index += 1
    if strict:
        name = values.get("name", "").strip()
        description = values.get("description", "").strip()
        if not SKILL_NAME_RE.fullmatch(name):
            raise SkillError("SKILL.md frontmatter 中的 name 格式不正确")
        if not description:
            raise SkillError("SKILL.md frontmatter 缺少 description")
        if len(description) > 1024:
            raise SkillError("Skill description 不能超过 1024 个字符")
    return values


def _validate_content(content: Any, *, expected_name: str) -> str:
    if not isinstance(content, str):
        raise SkillError("SKILL.md 内容必须是字符串")
    if "\x00" in content:
        raise SkillError("SKILL.md 不能包含空字符")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillError("SKILL.md 不能超过 256 KiB")
    values = _frontmatter(normalized, strict=True)
    if values["name"] != expected_name:
        raise SkillError("SKILL.md frontmatter 中的 name 必须与 Skill 目录名一致")
    return normalized.rstrip() + "\n"


def _render_skill(name: str, description: str, instructions: str) -> str:
    body = str(instructions or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise SkillError("Skill 指令不能为空")
    content = (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"{body}\n"
    )
    return _validate_content(content, expected_name=name)


def _validate_github_source(payload: dict[str, Any]) -> dict[str, str]:
    repository_url = _required(payload, "repository_url", "GitHub 仓库地址", 512)
    try:
        parsed = urlsplit(repository_url)
    except ValueError as exc:
        raise SkillError("GitHub 仓库地址格式不正确") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SkillError("目前只支持不带参数的公开 GitHub HTTPS 仓库地址")
    components = [item for item in parsed.path.split("/") if item]
    if len(components) != 2:
        raise SkillError("GitHub 仓库地址应为 https://github.com/<owner>/<repo>")
    owner, repository = components
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not GITHUB_OWNER_RE.fullmatch(owner) or not GITHUB_REPOSITORY_RE.fullmatch(repository):
        raise SkillError("GitHub owner 或仓库名称格式不正确")

    skill_path = _required(payload, "skill_path", "仓库内 Skill 路径", 512)
    if (
        skill_path.startswith("/")
        or "\\" in skill_path
        or "\x00" in skill_path
        or any(ord(character) < 32 for character in skill_path)
    ):
        raise SkillError("仓库内 Skill 路径必须是安全的相对 POSIX 路径")
    path = PurePosixPath(skill_path)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillError("仓库内 Skill 路径不能包含空段、. 或 ..")
    normalized_path = path.as_posix()
    name = _validate_skill_name(path.name)

    ref = str(payload.get("ref") or "main").strip()
    if (
        not GITHUB_REF_RE.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise SkillError("GitHub 分支、标签或提交格式不正确")
    return {
        "repository_url": f"https://github.com/{owner}/{repository}",
        "repository": f"{owner}/{repository}",
        "skill_path": normalized_path,
        "name": name,
        "ref": ref,
    }


def _read_origin(directory: Path) -> dict[str, Any] | None:
    path = directory / ORIGIN_NAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        data = json.loads(_read_limited(path, 16 * 1024, label="Skill 来源记录"))
    except (SkillError, ValueError, TypeError):
        return {
            "provider": "invalid",
            "validation_error": "Skill 来源记录已损坏或不安全",
        }
    if not isinstance(data, dict) or data.get("provider") != "github":
        return {
            "provider": "invalid",
            "validation_error": "Skill 来源记录格式不正确",
        }
    allowed = {
        "provider",
        "repository_url",
        "repository",
        "skill_path",
        "ref",
        "installed_at",
        "content_sha256",
        "file_count",
        "total_size",
        "dependency_files",
        "dependencies_installed_by_manager",
    }
    return {key: data[key] for key in allowed if key in data}


def _inspect_github_package(directory: Path, *, expected_name: str) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise SkillError("GitHub Skill 暂存目录不安全", "unsafe")
    if (directory / ORIGIN_NAME).exists() or (directory / ORIGIN_NAME).is_symlink():
        raise SkillError(f"GitHub Skill 不能包含保留文件 {ORIGIN_NAME}", "unsafe")

    skill_content = _read_limited(directory / "SKILL.md", MAX_SKILL_BYTES, label="SKILL.md")
    values = _frontmatter(skill_content, strict=True)
    if values.get("name") != expected_name:
        raise SkillError("GitHub Skill 的 frontmatter name 必须与目录名一致")

    digest = hashlib.sha256()
    file_count = 0
    total_size = 0
    executable_files = 0
    dependency_files: list[str] = []
    dependency_names = {"requirements.txt", "pyproject.toml", "package.json"}
    for root, directories, files in os.walk(directory, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            info = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise SkillError("GitHub Skill 不能包含符号链接或特殊目录", "unsafe")
        for name in files:
            child = root_path / name
            relative = child.relative_to(directory).as_posix()
            if len(relative) > 1024 or any(ord(character) < 32 for character in relative):
                raise SkillError("GitHub Skill 包含不安全或过长的文件名", "unsafe")
            info = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise SkillError("GitHub Skill 不能包含符号链接或特殊文件", "unsafe")
            if info.st_size > MAX_GITHUB_FILE_BYTES:
                raise SkillError("GitHub Skill 中存在超过 64 MiB 的单个文件")
            file_count += 1
            total_size += info.st_size
            if file_count > MAX_GITHUB_PACKAGE_FILES:
                raise SkillError("GitHub Skill 文件数量超过 25000 个")
            if total_size > MAX_GITHUB_PACKAGE_BYTES:
                raise SkillError("GitHub Skill 解压后总大小超过 512 MiB")
            if info.st_mode & 0o111:
                executable_files += 1
            if name.lower() in dependency_names and len(dependency_files) < 50:
                dependency_files.append(relative)

            descriptor = -1
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            try:
                descriptor = os.open(
                    child,
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                remaining = info.st_size
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise SkillError("读取 GitHub Skill 文件时内容发生变化", "conflict")
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if after.st_size != info.st_size or (os.name != "nt" and after.st_ino != info.st_ino):
                raise SkillError("校验 GitHub Skill 时文件发生变化，请重试", "conflict")
            digest.update(b"\0")

    return {
        "content_sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_size": total_size,
        "executable_files": executable_files,
        "dependency_files": dependency_files,
    }


def _github_installer(context: dict[str, Any]) -> Path:
    relative = Path("skills/.system/skill-installer/scripts/install-skill-from-github.py")
    candidates = [context["codex_home"] / relative, context["home"] / ".codex" / relative]
    for installer in candidates:
        try:
            if installer.is_symlink() or not installer.is_file():
                continue
            if installer.resolve(strict=True) == installer:
                return installer
        except OSError:
            continue
    raise SkillError("系统 skill-installer 不存在或路径不安全", "unavailable")


def _installer_environment(*, isolated_home: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.setdefault("PATH", os.defpath)
    environment["HOME"] = str(isolated_home)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=5"
    return environment


def _read_state(context: dict[str, Any]) -> dict[str, Any]:
    path: Path = context["state"]
    if not path.exists():
        return {"version": 1, "disabled": []}
    try:
        data = json.loads(_read_limited(path, 64 * 1024, label="Skill 管理状态"))
    except (ValueError, TypeError) as exc:
        raise SkillError("Skill 管理状态已损坏") from exc
    if not isinstance(data, dict):
        raise SkillError("Skill 管理状态已损坏")
    raw_disabled = data.get("disabled") or []
    if not isinstance(raw_disabled, list) or any(
        not isinstance(item, str) or not SKILL_NAME_RE.fullmatch(item)
        for item in raw_disabled
    ):
        raise SkillError("Skill 管理状态中的 disabled 列表无效")
    disabled = sorted(set(raw_disabled))
    return {"version": 1, "disabled": disabled}


def _write_state(context: dict[str, Any], state: dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "disabled": sorted(set(state.get("disabled") or [])),
        "updated_at": _utc_now(),
    }
    _atomic_write(context["state"], json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _managed_config_block(context: dict[str, Any], disabled: list[str]) -> str:
    lines = [CONFIG_BEGIN]
    for name in sorted(set(disabled)):
        path = context["skills"] / name / "SKILL.md"
        lines.extend(
            [
                "[[skills.config]]",
                f"path = {json.dumps(str(path), ensure_ascii=False)}",
                "enabled = false",
                "",
            ]
        )
    if not disabled:
        lines.append("# No disabled Profile Skills.")
    lines.append(CONFIG_END)
    return "\n".join(lines)


def _sync_config(context: dict[str, Any], state: dict[str, Any]) -> None:
    path: Path = context["config"]
    content = _read_limited(path, MAX_CONFIG_BYTES, label="Profile config.toml")
    pattern = re.compile(
        rf"(?:\n+)?{re.escape(CONFIG_BEGIN)}.*?{re.escape(CONFIG_END)}(?:\n+)?",
        re.DOTALL,
    )
    base = pattern.sub("\n", content).rstrip()
    block = _managed_config_block(context, list(state.get("disabled") or []))
    _atomic_write(path, f"{base}\n\n{block}\n", 0o600)


def _save_state_and_config(
    context: dict[str, Any], old_state: dict[str, Any], new_state: dict[str, Any]
) -> None:
    _write_state(context, new_state)
    try:
        _sync_config(context, new_state)
    except Exception:
        _write_state(context, old_state)
        try:
            _sync_config(context, old_state)
        except Exception:
            pass
        raise


def _skill_path(context: dict[str, Any], scope: str, name: str) -> Path:
    if scope not in {"profile", "system"}:
        raise SkillError("Skill scope 必须是 profile 或 system")
    validated = _validate_skill_name(name)
    root: Path = context["skills"] if scope == "profile" else context["skills"] / ".system"
    if root.is_symlink():
        raise SkillError("Skill 目录不安全", "unsafe")
    directory = root / validated
    if directory.is_symlink() or not directory.is_dir():
        raise SkillError("Skill 不存在或目录不安全", "not_found")
    return directory / "SKILL.md"


def _metadata(context: dict[str, Any], scope: str, directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    path = directory / "SKILL.md"
    content = _read_limited(path, MAX_SKILL_BYTES, label="SKILL.md")
    validation_error = ""
    try:
        values = _frontmatter(content, strict=True)
        if values.get("name") != directory.name:
            raise SkillError("SKILL.md frontmatter 中的 name 与目录名不一致")
    except SkillError as exc:
        values = _frontmatter(content, strict=False)
        validation_error = str(exc)
    try:
        supporting = any(item.name != "SKILL.md" for item in directory.iterdir())
    except OSError:
        supporting = False
    info = path.stat()
    name = directory.name
    origin = _read_origin(directory) if scope == "profile" else None
    return {
        "name": name,
        "manifest_name": str(values.get("name") or name),
        "description": str(values.get("description") or "未提供 description"),
        "scope": scope,
        "enabled": scope == "system" or name not in set(state.get("disabled") or []),
        "editable": scope == "profile",
        "valid": not validation_error,
        "validation_error": validation_error,
        "has_supporting_files": supporting,
        "size": info.st_size,
        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        "origin": origin,
    }


def _scan_root(
    context: dict[str, Any], scope: str, root: Path, state: dict[str, Any]
) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not SKILL_NAME_RE.fullmatch(directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            result.append(_metadata(context, scope, directory, state))
        except SkillError:
            continue
    return result


def list_skills(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    state = _read_state(context)
    custom = _scan_root(context, "profile", context["skills"], state)
    system = _scan_root(context, "system", context["skills"] / ".system", state)
    trash = _list_trash(context)
    return {
        "profile_id": context["profile_name"],
        "skills": custom + system,
        "counts": {
            "profile": len(custom),
            "system": len(system),
            "disabled": sum(1 for item in custom if not item["enabled"]),
            "trash": len(trash),
        },
    }


def get_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    scope = str(payload.get("scope") or "")
    name = _validate_skill_name(payload.get("name"))
    path = _skill_path(context, scope, name)
    state = _read_state(context)
    metadata = _metadata(context, scope, path.parent, state)
    return {**metadata, "content": _read_limited(path, MAX_SKILL_BYTES, label="SKILL.md")}


def _audit(context: dict[str, Any], action: str, name: str, **details: Any) -> None:
    record = {
        "at": _utc_now(),
        "profile_id": context["profile_name"],
        "action": action,
        "skill": name,
        **details,
    }
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        descriptor = os.open(
            context["audit"],
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(line):
                written = os.write(descriptor, line[offset:])
                if written <= 0:
                    raise OSError("audit write returned zero bytes")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The requested mutation already succeeded.  Audit storage must not turn
        # that into a misleading API failure.
        pass


def create_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    name = _validate_skill_name(payload.get("name"))
    description = _required(payload, "description", "Skill description", 1024)
    content = _render_skill(name, description, str(payload.get("instructions") or ""))
    with _mutation_lock(context):
        state = _read_state(context)
        root: Path = context["skills"]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = root / name
        if directory.exists() or directory.is_symlink():
            raise SkillError("同名 Profile Skill 已存在", "conflict")
        system_directory = root / ".system" / name
        if system_directory.exists() or system_directory.is_symlink():
            raise SkillError("同名系统 Skill 已存在，请换一个名称", "conflict")
        directory.mkdir(mode=0o700)
        try:
            _atomic_write(directory / "SKILL.md", content, 0o600)
        except Exception:
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        _audit(context, "create", name)
        return _metadata(context, "profile", directory, state)


def _clean_staging(container: Path, staging_root: Path) -> None:
    try:
        if container.is_symlink():
            container.unlink()
        elif container.exists():
            shutil.rmtree(container)
    except OSError:
        pass
    try:
        staging_root.rmdir()
    except OSError:
        pass


def install_github_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    source = _validate_github_source(payload)
    name = source["name"]
    with _mutation_lock(context):
        state = _read_state(context)
        root: Path = context["skills"]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise SkillError("Profile Skill 根目录不安全", "unsafe")
        target = root / name
        if target.exists() or target.is_symlink():
            raise SkillError("同名 Profile Skill 已存在", "conflict")
        system_target = root / ".system" / name
        if system_target.exists() or system_target.is_symlink():
            raise SkillError("同名系统 Skill 已存在，请换一个 Skill", "conflict")

        installer = _github_installer(context)
        staging_root: Path = context["staging"]
        if staging_root.is_symlink():
            raise SkillError("GitHub Skill 暂存根目录不安全", "unsafe")
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if staging_root.is_symlink() or not staging_root.is_dir():
            raise SkillError("GitHub Skill 暂存根目录不安全", "unsafe")
        os.chmod(staging_root, 0o700)
        container = staging_root / f"{_stamp()}-{uuid4().hex}"
        container.mkdir(mode=0o700)
        installer_home = container / ".installer-home"
        installer_home.mkdir(mode=0o700)
        staged = container / name
        command = [
            sys.executable,
            str(installer),
            "--repo",
            source["repository"],
            "--path",
            source["skill_path"],
            "--ref",
            source["ref"],
            "--dest",
            str(container),
            "--method",
            "git",
        ]
        try:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=GITHUB_INSTALL_TIMEOUT_SECONDS,
                    check=False,
                    cwd=installer.parent,
                    env=_installer_environment(isolated_home=installer_home),
                )
            except subprocess.TimeoutExpired as exc:
                raise SkillError("GitHub Skill 下载超过 5 分钟，已取消", "timeout") from exc
            except OSError as exc:
                raise SkillError(f"无法启动系统 skill-installer：{exc}", "unavailable") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "GitHub 下载失败").strip()[-1500:]
                detail = detail.replace(str(context["codex_home"]), "<PROFILE_HOME>")
                raise SkillError(f"GitHub Skill 下载失败：{detail}", "download_failed")
            if target.exists() or target.is_symlink():
                raise SkillError("安装期间出现同名 Profile Skill", "conflict")

            inventory = _inspect_github_package(staged, expected_name=name)
            origin = {
                "version": 1,
                "provider": "github",
                "repository_url": source["repository_url"],
                "repository": source["repository"],
                "skill_path": source["skill_path"],
                "ref": source["ref"],
                "installed_at": _utc_now(),
                "content_sha256": inventory["content_sha256"],
                "file_count": inventory["file_count"],
                "total_size": inventory["total_size"],
                "dependency_files": inventory["dependency_files"],
                "dependencies_installed_by_manager": False,
            }
            _atomic_write(staged / ORIGIN_NAME, json.dumps(origin, ensure_ascii=False, indent=2) + "\n")
            os.replace(staged, target)

            disabled = set(state.get("disabled") or [])
            disabled.discard(name)
            new_state = {"version": 1, "disabled": sorted(disabled)}
            if new_state["disabled"] != state["disabled"]:
                try:
                    _save_state_and_config(context, state, new_state)
                except Exception:
                    os.replace(target, staged)
                    raise
            metadata = _metadata(context, "profile", target, new_state)
            _audit(
                context,
                "install-github",
                name,
                repository=source["repository"],
                skill_path=source["skill_path"],
                ref=source["ref"],
                content_sha256=inventory["content_sha256"],
                file_count=inventory["file_count"],
                total_size=inventory["total_size"],
                executable_files=inventory["executable_files"],
                dependency_files=inventory["dependency_files"],
            )
            return {
                **metadata,
                "install": {
                    "executed_third_party_code": False,
                    "installed_dependencies": False,
                    "executable_files": inventory["executable_files"],
                    "dependency_files": inventory["dependency_files"],
                },
            }
        finally:
            _clean_staging(container, staging_root)


def update_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    name = _validate_skill_name(payload.get("name"))
    content = _validate_content(payload.get("content"), expected_name=name)
    with _mutation_lock(context):
        path = _skill_path(context, "profile", name)
        state = _read_state(context)
        previous = _read_limited(path, MAX_SKILL_BYTES, label="SKILL.md")
        if previous == content:
            return _metadata(context, "profile", path.parent, state)
        history: Path = context["history"] / name
        if history.is_symlink():
            raise SkillError("Skill 历史目录不安全", "unsafe")
        history.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = history / f"{_stamp()}-{uuid4().hex[:8]}.md"
        _atomic_write(backup, previous, 0o600)
        _atomic_write(path, content, 0o600)
        _audit(context, "update", name, backup=backup.name)
        return _metadata(context, "profile", path.parent, state)


def set_enabled(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    name = _validate_skill_name(payload.get("name"))
    if not isinstance(payload.get("enabled"), bool):
        raise SkillError("enabled 必须是布尔值")
    enabled = bool(payload["enabled"])
    with _mutation_lock(context):
        path = _skill_path(context, "profile", name)
        old_state = _read_state(context)
        disabled = set(old_state.get("disabled") or [])
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        new_state = {"version": 1, "disabled": sorted(disabled)}
        if new_state["disabled"] != old_state["disabled"]:
            _save_state_and_config(context, old_state, new_state)
            _audit(context, "enable" if enabled else "disable", name)
        return _metadata(context, "profile", path.parent, new_state)


def archive_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    name = _validate_skill_name(payload.get("name"))
    with _mutation_lock(context):
        path = _skill_path(context, "profile", name)
        source = path.parent
        trash_root: Path = context["trash"]
        if trash_root.is_symlink():
            raise SkillError("Skill 回收站目录不安全", "unsafe")
        trash_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        trash_id = f"{_stamp()}-{name}-{uuid4().hex[:8]}"
        container = trash_root / trash_id
        container.mkdir(mode=0o700)
        destination = container / "skill"
        old_state = _read_state(context)
        was_enabled = name not in set(old_state.get("disabled") or [])
        metadata = {
            "trash_id": trash_id,
            "name": name,
            "deleted_at": _utc_now(),
            "was_enabled": was_enabled,
        }
        try:
            os.replace(source, destination)
            _atomic_write(container / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
            disabled = set(old_state.get("disabled") or [])
            disabled.discard(name)
            new_state = {"version": 1, "disabled": sorted(disabled)}
            if new_state["disabled"] != old_state["disabled"]:
                _save_state_and_config(context, old_state, new_state)
        except Exception:
            if destination.exists() and not source.exists():
                os.replace(destination, source)
            try:
                (container / "metadata.json").unlink()
            except OSError:
                pass
            try:
                container.rmdir()
            except OSError:
                pass
            raise
        _audit(context, "archive", name, trash_id=trash_id)
        return metadata


def _list_trash(context: dict[str, Any]) -> list[dict[str, Any]]:
    root: Path = context["trash"]
    if root.is_symlink() or not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for container in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        if not TRASH_ID_RE.fullmatch(container.name) or container.is_symlink() or not container.is_dir():
            continue
        try:
            data = json.loads(_read_limited(container / "metadata.json", 16 * 1024, label="回收站记录"))
        except (SkillError, ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("trash_id") != container.name:
            continue
        name = str(data.get("name") or "")
        if not SKILL_NAME_RE.fullmatch(name):
            continue
        skill_directory = container / "skill"
        if skill_directory.is_symlink() or not skill_directory.is_dir():
            continue
        records.append(
            {
                "trash_id": container.name,
                "name": name,
                "deleted_at": str(data.get("deleted_at") or ""),
                "was_enabled": bool(data.get("was_enabled", True)),
            }
        )
    return records


def list_trash(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    records = _list_trash(context)
    return {"profile_id": context["profile_name"], "items": records, "count": len(records)}


def restore_skill(payload: dict[str, Any], *, home: Path) -> dict[str, Any]:
    context = _context(payload, home=home)
    trash_id = str(payload.get("trash_id") or "").strip()
    if not TRASH_ID_RE.fullmatch(trash_id):
        raise SkillError("回收站记录 ID 格式不正确")
    with _mutation_lock(context):
        container: Path = context["trash"] / trash_id
        if container.is_symlink() or not container.is_dir():
            raise SkillError("回收站记录不存在", "not_found")
        try:
            data = json.loads(_read_limited(container / "metadata.json", 16 * 1024, label="回收站记录"))
        except (ValueError, TypeError) as exc:
            raise SkillError("回收站记录已损坏") from exc
        name = _validate_skill_name(data.get("name"))
        source = container / "skill"
        if source.is_symlink() or not source.is_dir():
            raise SkillError("回收站中的 Skill 目录不安全", "unsafe")
        target: Path = context["skills"] / name
        if target.exists() or target.is_symlink():
            raise SkillError("同名 Profile Skill 已存在，无法恢复", "conflict")
        old_state = _read_state(context)
        try:
            os.replace(source, target)
            disabled = set(old_state.get("disabled") or [])
            if not bool(data.get("was_enabled", True)):
                disabled.add(name)
            new_state = {"version": 1, "disabled": sorted(disabled)}
            if new_state["disabled"] != old_state["disabled"]:
                _save_state_and_config(context, old_state, new_state)
        except Exception:
            if target.exists() and not source.exists():
                os.replace(target, source)
            raise
        try:
            (container / "metadata.json").unlink()
            container.rmdir()
        except OSError:
            pass
        _audit(context, "restore", name, trash_id=trash_id)
        return _metadata(context, "profile", target, new_state)


def _request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (ValueError, TypeError) as exc:
        raise SkillError("请求体必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise SkillError("请求体必须是 JSON 对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=(
            "list",
            "get",
            "create",
            "install-github",
            "update",
            "set-enabled",
            "archive",
            "trash",
            "restore",
        ),
        required=True,
    )
    parser.add_argument("--home", help=argparse.SUPPRESS)
    options = parser.parse_args()
    home = Path(options.home).expanduser() if options.home else Path.home()
    actions = {
        "list": list_skills,
        "get": get_skill,
        "create": create_skill,
        "install-github": install_github_skill,
        "update": update_skill,
        "set-enabled": set_enabled,
        "archive": archive_skill,
        "trash": list_trash,
        "restore": restore_skill,
    }
    try:
        result = actions[options.action](_request(), home=home)
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (SkillError, OSError) as exc:
        code = exc.code if isinstance(exc, SkillError) else "io_error"
        print(
            json.dumps(
                {"status": "error", "code": code, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
