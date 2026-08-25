"""Versioned project backup, validation and offline restore service.

Backup format v1 intentionally includes ``.env`` in plaintext because the
operator requested complete machine migration before encrypted archives are
introduced.  Every manifest and API response marks that fact prominently so a
plain archive is never mistaken for a secret-safe export.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from app.version import APP_VERSION, BACKUP_FORMAT_VERSION, PLUGIN_RUNTIME_API_VERSION


logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupOptions:
    profile: str = "state"
    include_models: bool = False
    include_diagnostics: bool = False
    include_machine_bound: bool = False
    include_generated: bool = True

    def normalized(self) -> "BackupOptions":
        if self.profile not in {"state", "migration"}:
            raise BackupError("备份类型只能是 state 或 migration")
        return self


class BackupService:
    FORMAT_NAME = "ggbot-backup"
    MANIFEST_NAME = "backup-manifest.json"
    PENDING_NAME = "pending_restore.json"
    ARCHIVE_SUFFIX = ".ggbot-backup.zip"

    STATE_ROOT_FILES = {
        ".env",
        ".env.example",
        "requirements.txt",
        "requirements-dev.txt",
    }
    STATE_DATA_DIRECTORIES = {
        "chat_logs",
        "chat_summaries",
        "chatbot_anchor_contexts",
        "memory_backups",
        "plugins",
    }
    GENERATED_DATA_DIRECTORIES = {
        "daily_reports",
        "jr_inventory_report",
        "weekly_reports",
        "feishu_dashboard_preview",
    }
    DIAGNOSTIC_NAMES = {
        "llm_call_history.jsonl",
        "llm_chat_cache_diagnostics.jsonl",
    }
    MACHINE_BOUND_DATA_DIRECTORIES = {
        "chrome_profile",
        "asr_cache",
    }
    MIGRATION_CODE_ROOTS = {
        "app",
        "web",
        "scripts",
        "config",
    }
    MIGRATION_ROOT_FILES = {
        "start.py",
        "wx_bot.py",
        "wechat_auto_login.py",
        "launcher.py",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        ".env",
        ".env.example",
        "cookies.txt",
    }
    MIGRATION_GENERATED_ROOTS = {"wxautox文件下载"}
    PPT_MASTER_ARCHIVE_ROOT = PurePosixPath(".codex/skills/ppt-master")
    SKIP_PARTS = {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".git",
        ".venv",
        "node_modules",
    }
    SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".temp", ".log"}

    def __init__(
        self,
        project_root: Optional[Path] = None,
        backup_root: Optional[Path] = None,
        ppt_master_dir: Optional[Path] = None,
    ):
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        configured = backup_root or Path(os.getenv("SYSTEM_BACKUP_DIR") or "data/system_backups")
        if not configured.is_absolute():
            configured = self.project_root / configured
        self.backup_root = configured.resolve()
        self.incoming_root = self.backup_root / "incoming"
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.incoming_root.mkdir(parents=True, exist_ok=True)
        configured_skill = str(
            ppt_master_dir or os.getenv("SYSTEM_BACKUP_PPT_MASTER_DIR") or ""
        ).strip()
        self.ppt_master_dir: Optional[Path] = None
        self.ppt_master_config_error: Optional[str] = None
        self._ppt_master_requested = bool(configured_skill)
        if configured_skill:
            try:
                self.ppt_master_dir = self._resolve_ppt_master_dir(configured_skill)
            except BackupError as exc:
                self.ppt_master_config_error = str(exc)
        self._lock = threading.RLock()

    @staticmethod
    def _resolve_ppt_master_dir(configured: str) -> Path:
        """Resolve the configured global PPT Master directory.

        ``wsl`` is a portable marker for the Codex installation used by this
        project: on Windows it resolves the default WSL user's Codex home; when
        the service itself runs in WSL it resolves the current Linux home.
        """
        if configured.strip().lower() != "wsl":
            return Path(configured).expanduser().resolve()

        if os.name != "nt":
            codex_home = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex"))
            return (codex_home / "skills" / "ppt-master").expanduser().resolve()

        try:
            locate = subprocess.run(
                [
                    "wsl.exe",
                    "bash",
                    "-lic",
                    'printf "%s" "${CODEX_HOME:-$HOME/.codex}/skills/ppt-master"',
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            linux_path = (locate.stdout or "").strip()
            if locate.returncode != 0 or not linux_path:
                detail = (locate.stderr or locate.stdout or "无法定位 WSL Codex 目录").strip()
                raise BackupError(detail[-1000:])
            convert = subprocess.run(
                ["wsl.exe", "wslpath", "-w", linux_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            windows_path = (convert.stdout or "").strip()
            if convert.returncode != 0 or not windows_path:
                detail = (convert.stderr or convert.stdout or "无法转换 WSL Codex 目录").strip()
                raise BackupError(detail[-1000:])
            return Path(windows_path).resolve()
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError(f"无法访问 WSL 中的 PPT Master 技能: {exc}") from exc

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_archive_name(value: str) -> str:
        name = Path(str(value or "")).name
        if (
            name != value
            or not name.endswith(BackupService.ARCHIVE_SUFFIX)
            or re.fullmatch(r"[A-Za-z0-9._-]+", name) is None
        ):
            raise BackupError("备份文件名无效")
        return name

    def archive_path(self, name: str) -> Path:
        safe_name = self._safe_archive_name(name)
        for root in (self.backup_root, self.incoming_root):
            candidate = root / safe_name
            if candidate.is_file():
                return candidate
        raise BackupError("备份文件不存在")

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            pass
        if self.ppt_master_dir is not None:
            try:
                skill_relative = resolved.relative_to(self.ppt_master_dir.resolve())
                return (self.PPT_MASTER_ARCHIVE_ROOT / skill_relative.as_posix()).as_posix()
            except ValueError:
                pass
        raise BackupError(f"备份源不在允许的项目或 Codex 技能目录内: {path}")

    def _is_backup_internal(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.backup_root)
            return True
        except ValueError:
            return False

    def _skip_common(self, path: Path) -> bool:
        if self._is_backup_internal(path):
            return True
        relative = Path(self._relative(path))
        if any(part in self.SKIP_PARTS for part in relative.parts):
            return True
        return path.suffix.lower() in self.SKIP_SUFFIXES or path.name.endswith(("-wal", "-shm"))

    def _iter_tree(self, root: Path) -> Iterator[Path]:
        if not root.exists():
            return
        if root.is_file():
            if not self._skip_common(root):
                yield root
            return
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and not self._skip_common(path):
                yield path

    def _iter_managed_plugin_data(
        self,
        root: Path,
        *,
        include_machine_bound: bool,
        include_generated: bool,
    ) -> Iterator[Path]:
        """Back up only the durable namespace provided by PluginContext.

        Cache and temporary directories are intentionally lifecycle-managed
        runtime data.  Keeping this rule in the backup collector means every
        Runtime API v2 plugin inherits the same migration behaviour without
        needing plugin-specific backup code.
        """
        if not root.exists():
            return
        for path in root.iterdir():
            if path.is_file() and not path.is_symlink() and not self._skip_common(path):
                yield path
                continue
            if not path.is_dir():
                continue
            persistent = path / "persistent"
            if persistent.exists():
                yield from self._iter_tree(persistent)
            generated = path / "generated"
            if include_generated and generated.exists():
                yield from self._iter_tree(generated)
            machine_bound = path / "machine_bound"
            if include_machine_bound and machine_bound.exists():
                yield from self._iter_tree(machine_bound)

    def _iter_state_data(self, options: BackupOptions) -> Iterator[Path]:
        data_root = self.project_root / "data"
        if not data_root.exists():
            return
        for path in data_root.iterdir():
            if self._is_backup_internal(path):
                continue
            if path.name == "system_trash":
                continue
            if path.name == "models" and not options.include_models:
                continue
            if path.name in self.MACHINE_BOUND_DATA_DIRECTORIES and not options.include_machine_bound:
                continue
            if path.name in self.GENERATED_DATA_DIRECTORIES and not options.include_generated:
                continue
            if path.name.startswith("corrupt_telemetry") and not options.include_diagnostics:
                continue
            if path.is_dir():
                if options.profile == "state" and path.name not in (
                    self.STATE_DATA_DIRECTORIES
                    | self.GENERATED_DATA_DIRECTORIES
                    | ({"models"} if options.include_models else set())
                    | (self.MACHINE_BOUND_DATA_DIRECTORIES if options.include_machine_bound else set())
                ):
                    continue
                if path.name == "plugins":
                    yield from self._iter_managed_plugin_data(
                        path,
                        include_machine_bound=options.include_machine_bound,
                        include_generated=options.include_generated,
                    )
                    continue
                yield from self._iter_tree(path)
                continue
            if path.name in self.DIAGNOSTIC_NAMES and not options.include_diagnostics:
                continue
            if not self._skip_common(path):
                yield path

    def _iter_plugin_contract_files(self) -> Iterator[Path]:
        plugins = self.project_root / "app" / "plugins"
        if not plugins.exists():
            return
        for name in ("routing_order.json",):
            path = plugins / name
            if path.is_file():
                yield path
        for pattern in ("*/config.json", "*/manifest.json"):
            for path in plugins.glob(pattern):
                if path.is_file():
                    yield path

    def collect_sources(self, options: BackupOptions) -> List[Path]:
        options = options.normalized()
        sources: Dict[str, Path] = {}

        def add(paths: Iterable[Path]) -> None:
            for path in paths:
                if path.is_file() and not self._skip_common(path):
                    sources[self._relative(path)] = path

        add(self.project_root / name for name in self.STATE_ROOT_FILES if (self.project_root / name).is_file())
        add(self._iter_state_data(options))
        add(self._iter_plugin_contract_files())

        if options.profile == "migration":
            add(self.project_root / name for name in self.MIGRATION_ROOT_FILES if (self.project_root / name).is_file())
            for name in self.MIGRATION_CODE_ROOTS:
                root = self.project_root / name
                if root.exists():
                    add(self._iter_tree(root))
            if options.include_generated:
                for name in self.MIGRATION_GENERATED_ROOTS:
                    root = self.project_root / name
                    if root.exists():
                        add(self._iter_tree(root))
            if self._ppt_master_requested:
                if self.ppt_master_config_error:
                    raise BackupError(
                        f"PPT Master 备份目录配置无效: {self.ppt_master_config_error}"
                    )
                skill_root = self.ppt_master_dir
                if skill_root is None or not (skill_root / "SKILL.md").is_file():
                    raise BackupError(
                        f"PPT Master 技能目录不存在或缺少 SKILL.md: {skill_root}"
                    )
                add(self._iter_tree(skill_root))

        return [sources[key] for key in sorted(sources)]

    @staticmethod
    def _is_sqlite(path: Path) -> bool:
        if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"} or not path.is_file():
            return False
        try:
            with path.open("rb") as handle:
                return handle.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    @staticmethod
    def _sqlite_snapshot(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
        target_connection = sqlite3.connect(str(destination), timeout=30)
        try:
            source_connection.execute("PRAGMA busy_timeout=30000")
            source_connection.backup(target_connection, pages=1024, sleep=0.01)
        finally:
            target_connection.close()
            source_connection.close()

    def _prepared_source(self, source: Path, staging: Path) -> Tuple[Path, str]:
        relative = self._relative(source)
        snapshot = staging / relative
        if self._is_sqlite(source):
            self._sqlite_snapshot(source, snapshot)
            return snapshot, "sqlite_online_backup"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, snapshot)
        return snapshot, "file_snapshot"

    @staticmethod
    def _classification(relative: str) -> Dict[str, Any]:
        relative_path = PurePosixPath(relative)
        sensitive = (
            relative_path.name == ".env"
            or "cookie" in relative.lower()
            or "secret" in relative.lower()
        )
        parts = PurePosixPath(relative).parts
        machine_bound = relative.startswith(("data/chrome_profile/", "data/asr_cache/")) or (
            len(parts) >= 4
            and parts[:2] == ("data", "plugins")
            and parts[3] == "machine_bound"
        )
        generated = relative.startswith(
            ("data/weekly_reports/", "data/daily_reports/", "data/jr_inventory_report/")
        ) or (
            len(parts) >= 4
            and parts[:2] == ("data", "plugins")
            and parts[3] == "generated"
        )
        owner = "core"
        if len(parts) >= 3 and parts[:2] == ("data", "plugins"):
            owner = f"plugin-storage:{parts[2]}"
        elif len(parts) >= 3 and parts[:2] == ("app", "plugins"):
            owner = f"plugin:{parts[2]}"
        elif parts[:3] == (".codex", "skills", "ppt-master"):
            owner = "codex-skill:ppt-master"
        return {
            "sensitive": sensitive,
            "portable": not machine_bound,
            "machine_bound": machine_bound,
            "generated": generated,
            "owner": owner,
        }

    def create_backup(self, options: BackupOptions, operation: Any = None) -> Dict[str, Any]:
        options = options.normalized()
        with self._lock:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"ggbot-{options.profile}-{timestamp}{self.ARCHIVE_SUFFIX}"
            destination = self.backup_root / name
            collision = 1
            while destination.exists():
                name = f"ggbot-{options.profile}-{timestamp}-{collision}{self.ARCHIVE_SUFFIX}"
                destination = self.backup_root / name
                collision += 1
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            sources = self.collect_sources(options)
            if not sources:
                raise BackupError("没有找到可备份的数据")
            if operation:
                operation.progress(2, "已完成备份清单", files=len(sources))

            records: List[Dict[str, Any]] = []
            total_bytes = 0
            with tempfile.TemporaryDirectory(prefix="backup-stage-", dir=self.backup_root) as staging_name:
                staging = Path(staging_name)
                try:
                    with zipfile.ZipFile(
                        temporary,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=3,
                        allowZip64=True,
                    ) as archive:
                        for index, source in enumerate(sources, 1):
                            if operation:
                                operation.check_cancelled()
                            relative = self._relative(source)
                            prepared, consistency = self._prepared_source(source, staging)
                            size = prepared.stat().st_size
                            classification = self._classification(relative)
                            record = {
                                "path": relative,
                                "bytes": size,
                                "sha256": self._sha256(prepared),
                                "consistency": consistency,
                                **classification,
                            }
                            archive.write(prepared, relative)
                            records.append(record)
                            total_bytes += size
                            if operation and (index == len(sources) or index % 20 == 0):
                                operation.progress(
                                    5 + int(index / len(sources) * 85),
                                    f"正在打包 {index}/{len(sources)}",
                                    current=relative,
                                    bytes=total_bytes,
                                )

                        manifest = {
                            "format": self.FORMAT_NAME,
                            "format_version": BACKUP_FORMAT_VERSION,
                            "created_at": self._utc_now(),
                            "app_version": APP_VERSION,
                            "plugin_runtime_api_version": PLUGIN_RUNTIME_API_VERSION,
                            "profile": options.profile,
                            "options": {
                                "include_models": options.include_models,
                                "include_diagnostics": options.include_diagnostics,
                                "include_machine_bound": options.include_machine_bound,
                                "include_generated": options.include_generated,
                            },
                            "security": {
                                "encrypted": False,
                                "contains_plaintext_env": any(
                                    PurePosixPath(item["path"]).name == ".env"
                                    for item in records
                                ),
                                "warning": "此备份格式未加密，包含的 .env、Cookie 和插件密钥均为明文。",
                            },
                            "consistency": {
                                "mode": "per_file_snapshot",
                                "sqlite_online_backup": True,
                                "legacy_plugin_writes_quiesced": False,
                            },
                            "file_count": len(records),
                            "total_bytes": total_bytes,
                            "files": records,
                        }
                        archive.writestr(
                            self.MANIFEST_NAME,
                            json.dumps(manifest, ensure_ascii=False, indent=2),
                        )
                    os.replace(temporary, destination)
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

            if operation:
                operation.progress(98, "正在校验迁移包")
            validation = self.validate_archive(destination, verify_files=False)
            result = {
                "name": name,
                "path": str(destination),
                "profile": options.profile,
                "bytes": destination.stat().st_size,
                "file_count": len(records),
                "created_at": manifest["created_at"],
                "security": manifest["security"],
                "valid": validation["valid"],
            }
            try:
                from app.services.runtime_operations import get_runtime_operation_service

                get_runtime_operation_service().record_audit(
                    category="backup",
                    action="create_backup",
                    target=name,
                    summary="创建状态备份" if options.profile == "state" else "创建完整迁移包",
                    after={"profile": options.profile, "bytes": result["bytes"], "file_count": len(records)},
                    details={"contains_plaintext_env": manifest["security"]["contains_plaintext_env"]},
                )
            except Exception:
                pass
            return result

    @staticmethod
    def _safe_member(member: str) -> str:
        path = PurePosixPath(member)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise BackupError(f"迁移包包含不安全路径: {member}")
        return path.as_posix()

    def read_manifest(self, archive_path: Path) -> Dict[str, Any]:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                payload = json.loads(archive.read(self.MANIFEST_NAME).decode("utf-8"))
        except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"无法读取备份清单: {exc}") from exc
        if payload.get("format") != self.FORMAT_NAME:
            raise BackupError("不是 GGBot 备份包")
        if int(payload.get("format_version") or 0) != BACKUP_FORMAT_VERSION:
            raise BackupError(f"不支持的备份格式版本: {payload.get('format_version')}")
        return payload

    def validate_archive(self, archive_path: Path, *, verify_files: bool = True, operation: Any = None) -> Dict[str, Any]:
        manifest = self.read_manifest(archive_path)
        expected = {str(item.get("path")): item for item in manifest.get("files", [])}
        errors: List[str] = []
        checked = 0
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = {self._safe_member(info.filename): info for info in archive.infolist()}
                for relative, record in expected.items():
                    self._safe_member(relative)
                    if relative not in members:
                        errors.append(f"缺少文件: {relative}")
                        continue
                    recorded_bytes = record.get("bytes")
                    if recorded_bytes is None or int(recorded_bytes) != members[relative].file_size:
                        errors.append(f"文件大小不匹配: {relative}")
                        continue
                    if verify_files:
                        digest = hashlib.sha256()
                        with archive.open(relative) as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                        if digest.hexdigest() != record.get("sha256"):
                            errors.append(f"校验和不匹配: {relative}")
                    checked += 1
                    if operation and checked % 20 == 0:
                        operation.progress(
                            min(95, int(checked / max(len(expected), 1) * 95)),
                            f"正在校验 {checked}/{len(expected)}",
                        )
        except (OSError, zipfile.BadZipFile, BackupError) as exc:
            errors.append(str(exc))
        return {
            "valid": not errors,
            "errors": errors,
            "checked_files": checked,
            "manifest": {key: value for key, value in manifest.items() if key != "files"},
            "security_warning": (manifest.get("security") or {}).get("warning"),
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        rows = []
        for source, imported in ((self.backup_root, False), (self.incoming_root, True)):
            for path in source.glob(f"*{self.ARCHIVE_SUFFIX}"):
                try:
                    manifest = self.read_manifest(path)
                    validation = self.validate_archive(path, verify_files=False)
                    rows.append(
                        {
                            "name": path.name,
                            "bytes": path.stat().st_size,
                            "created_at": manifest.get("created_at"),
                            "profile": manifest.get("profile"),
                            "app_version": manifest.get("app_version"),
                            "file_count": manifest.get("file_count"),
                            "encrypted": bool((manifest.get("security") or {}).get("encrypted")),
                            "contains_plaintext_env": bool((manifest.get("security") or {}).get("contains_plaintext_env")),
                            "imported": imported,
                            "valid": validation["valid"],
                            "error": "；".join(validation["errors"][:3]) if validation["errors"] else None,
                        }
                    )
                except BackupError as exc:
                    rows.append(
                        {
                            "name": path.name,
                            "bytes": path.stat().st_size,
                            "imported": imported,
                            "valid": False,
                            "error": str(exc),
                        }
                    )
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows

    def import_path(self, filename: str) -> Path:
        safe = self._safe_archive_name(filename)
        return self.incoming_root / safe

    def pending_path(self) -> Path:
        return self.backup_root / self.PENDING_NAME

    def delete_archive(self, archive_name: str, *, confirmation: str) -> Dict[str, Any]:
        """Permanently remove one managed backup archive.

        A backup referenced by the pending offline-restore plan is protected so
        the next application start cannot be left with a broken restore target.
        """
        if confirmation.strip() != "删除备份":
            raise BackupError("请输入“删除备份”确认操作")

        with self._lock:
            archive = self.archive_path(archive_name)
            pending_path = self.pending_path()
            if pending_path.exists():
                try:
                    pending = json.loads(pending_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise BackupError("待恢复计划无法读取，已阻止删除备份") from exc
                pending_name = str(pending.get("archive_name") or "")
                if not pending_name:
                    pending_name = Path(str(pending.get("archive") or "")).name
                if pending_name == archive.name:
                    raise BackupError("该备份正在用于待执行的恢复计划，不能删除")

            imported = archive.parent == self.incoming_root
            size = archive.stat().st_size
            profile = None
            try:
                profile = self.read_manifest(archive).get("profile")
            except BackupError:
                # Invalid archives must remain deletable from the management UI.
                pass
            archive.unlink()

        result = {
            "deleted": True,
            "name": archive.name,
            "bytes": size,
            "profile": profile,
            "imported": imported,
        }
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="backup",
                action="delete_backup",
                target=archive.name,
                summary="永久删除备份",
                before={
                    "profile": profile,
                    "bytes": size,
                    "imported": imported,
                },
                after={"exists": False},
            )
        except Exception:
            pass
        return result

    def prepare_restore(self, archive_name: str, *, confirmation: str) -> Dict[str, Any]:
        if confirmation.strip() != "恢复备份":
            raise BackupError("请输入“恢复备份”确认操作")
        archive = self.archive_path(archive_name)
        validation = self.validate_archive(archive, verify_files=True)
        if not validation["valid"]:
            raise BackupError("备份校验失败：" + "；".join(validation["errors"][:5]))
        pending = {
            "schema_version": 1,
            "archive": str(archive),
            "archive_name": archive.name,
            "prepared_at": self._utc_now(),
            "security_warning": validation.get("security_warning"),
        }
        temporary = self.pending_path().with_suffix(".tmp")
        temporary.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.pending_path())
        result = {
            "prepared": True,
            "archive_name": archive.name,
            "restart_required": True,
            "message": "恢复计划已创建；下一次启动将在加载应用前恢复。",
        }
        try:
            from app.services.runtime_operations import get_runtime_operation_service

            get_runtime_operation_service().record_audit(
                category="backup",
                action="prepare_restore",
                target=archive.name,
                summary="已创建离线恢复计划",
                details={"restart_required": True},
            )
        except Exception:
            pass
        return result

    def _extract_verified(self, archive_path: Path, destination: Path) -> Tuple[Dict[str, Any], List[str]]:
        validation = self.validate_archive(archive_path, verify_files=True)
        if not validation["valid"]:
            raise BackupError("备份校验失败：" + "；".join(validation["errors"][:5]))
        manifest = self.read_manifest(archive_path)
        restored: List[str] = []
        with zipfile.ZipFile(archive_path, "r") as archive:
            for record in manifest.get("files", []):
                relative = self._safe_member(str(record["path"]))
                target = destination / Path(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(relative) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                restored.append(relative)
        return manifest, restored

    def _restore_target(self, relative: str) -> Path:
        member = PurePosixPath(self._safe_member(relative))
        prefix = self.PPT_MASTER_ARCHIVE_ROOT.parts
        if member.parts[: len(prefix)] == prefix:
            if self.ppt_master_dir is None:
                detail = self.ppt_master_config_error or "未配置 SYSTEM_BACKUP_PPT_MASTER_DIR"
                raise BackupError(f"无法恢复 PPT Master 技能: {detail}")
            suffix = member.parts[len(prefix) :]
            if not suffix:
                raise BackupError("PPT Master 恢复条目缺少文件路径")
            skill_root = self.ppt_master_dir.resolve()
            target = (skill_root / Path(*suffix)).resolve()
            try:
                target.relative_to(skill_root)
            except ValueError as exc:
                raise BackupError(f"PPT Master 恢复目标越界: {relative}") from exc
            return target

        target = (self.project_root / Path(*member.parts)).resolve()
        try:
            target.relative_to(self.project_root)
        except ValueError as exc:
            raise BackupError(f"恢复目标越过项目目录: {relative}") from exc
        return target

    def restore_archive(self, archive_path: Path, *, create_safety_backup: bool = True) -> Dict[str, Any]:
        """Restore an archive while the application is stopped.

        Files are fully extracted and verified before any project path changes.
        A file-level rollback directory protects the current installation if an
        overwrite fails midway.
        """
        archive_path = archive_path.resolve()
        with self._lock, tempfile.TemporaryDirectory(prefix="restore-stage-", dir=self.backup_root) as stage_name:
            stage = Path(stage_name) / "payload"
            rollback = Path(stage_name) / "rollback"
            stage.mkdir(parents=True)
            manifest, relatives = self._extract_verified(archive_path, stage)

            safety_backup = None
            if create_safety_backup:
                safety_backup = self.create_backup(
                    BackupOptions(profile="state", include_generated=True),
                )

            existing: List[str] = []
            created: List[str] = []
            applied: List[str] = []
            try:
                for relative in relatives:
                    source = stage / Path(*PurePosixPath(relative).parts)
                    target = self._restore_target(relative)
                    if target.exists():
                        previous = rollback / Path(*PurePosixPath(relative).parts)
                        previous.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, previous)
                        existing.append(relative)
                    else:
                        created.append(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.restore.tmp")
                    shutil.copy2(source, temporary)
                    os.replace(temporary, target)
                    applied.append(relative)
            except Exception:
                for relative in reversed(applied):
                    target = self._restore_target(relative)
                    previous = rollback / Path(*PurePosixPath(relative).parts)
                    if relative in existing and previous.exists():
                        shutil.copy2(previous, target)
                    elif relative in created:
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                raise

            return {
                "restored": True,
                "archive": archive_path.name,
                "profile": manifest.get("profile"),
                "files": len(applied),
                "safety_backup": safety_backup,
                "security_warning": (manifest.get("security") or {}).get("warning"),
                "restored_at": self._utc_now(),
            }

    def apply_pending_restore(self) -> Optional[Dict[str, Any]]:
        pending_path = self.pending_path()
        if not pending_path.exists():
            return None
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            archive = Path(str(pending.get("archive") or ""))
            if not archive.is_file():
                raise BackupError("待恢复的备份文件不存在")
            result = self.restore_archive(archive, create_safety_backup=True)
            completed = self.backup_root / f"restore-applied-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            completed.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            pending_path.unlink()
            return result
        except Exception as exc:
            failed = dict(pending if "pending" in locals() else {})
            failed.update({"failed_at": self._utc_now(), "error": str(exc)})
            failed_path = self.backup_root / f"restore-failed-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            failed_path.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def overview(self) -> Dict[str, Any]:
        pending = None
        if self.pending_path().exists():
            try:
                pending = json.loads(self.pending_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = {"invalid": True}
        backups = self.list_backups()
        return {
            "format_version": BACKUP_FORMAT_VERSION,
            "backup_directory": str(self.backup_root),
            "security": {
                "encrypted": False,
                "env_included": True,
                "warning": "当前备份格式未加密；迁移包包含 .env 时应按敏感文件保管。",
                "encryption_planned": True,
            },
            "pending_restore": pending,
            "backups": backups,
            "counts": {
                "total": len(backups),
                "valid": sum(bool(item.get("valid")) for item in backups),
                "migration": sum(item.get("profile") == "migration" for item in backups),
            },
            "profiles": [
                {"id": "state", "title": "状态备份", "description": "数据库、配置、聊天与插件状态"},
                {"id": "migration", "title": "完整迁移", "description": "状态、当前代码、插件、Codex 技能和生成内容"},
            ],
        }


_service: Optional[BackupService] = None
_service_lock = threading.Lock()


def get_backup_service() -> BackupService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BackupService()
    return _service


def apply_pending_restore(project_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    return BackupService(project_root=project_root).apply_pending_restore()
