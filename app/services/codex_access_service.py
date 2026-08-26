"""Per-chat Codex access policy and durable workspace allocation.

Inbound chat text is not an authorization boundary.  This module resolves the
trusted policy stored by the Web administrator and turns it into runtime-only
Codex settings that user prompts cannot override.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.models.base import SessionLocal
from app.models.user_permission import WeChatUser
from app.services.wechat_file_store import PROJECT_ROOT, safe_path_component


ISOLATED_ACCESS = "isolated"
OWNER_FULL_ACCESS = "owner_full"
SUPPORTED_ACCESS_MODES = {ISOLATED_ACCESS, OWNER_FULL_ACCESS}
ISOLATED_PERMISSION_PROFILE = "wxautox-chat-isolated"
OWNER_PERMISSION_PROFILE = ":danger-full-access"
ACCESS_POLICY_VERSION = "chat-scope-v1"


def normalize_codex_access_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_ACCESS_MODES else ISOLATED_ACCESS


def _scope_base() -> Path:
    configured = str(os.getenv("CODEX_CHAT_SCOPE_ROOT") or "").strip()
    root = Path(configured).expanduser() if configured else PROJECT_ROOT / "data" / "codex_chat_scopes"
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve()


def chat_scope_path(chat_name: str, *, root: Optional[Path] = None) -> Path:
    """Return a stable, collision-resistant and human-readable chat directory."""
    normalized_name = str(chat_name or "").strip()
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:10]
    readable = safe_path_component(normalized_name, fallback="chat", max_length=72)
    return (Path(root or _scope_base()) / f"{readable}--{digest}").resolve()


@dataclass(frozen=True)
class CodexAccessContext:
    chat_name: str
    is_group: bool
    mode: str
    scope_root: Path
    workdir: Path
    permission_profile: str
    approval_policy: str
    config_policy: str
    persistent_thread: bool
    policy_version: str = ACCESS_POLICY_VERSION

    @property
    def is_owner(self) -> bool:
        return self.mode == OWNER_FULL_ACCESS

    @property
    def label(self) -> str:
        return "管理员 · 最大权限" if self.is_owner else "隔离空间"

    @property
    def scope_kind(self) -> str:
        if self.is_owner:
            return "local_full"
        return "group_shared" if self.is_group else "private_isolated"

    @property
    def artifact_root(self) -> Path:
        if self.is_owner:
            configured = str(os.getenv("CODEX_PROXY_ARTIFACT_ROOT") or "tmp/images/codex")
            root = Path(configured)
            return root if root.is_absolute() else (PROJECT_ROOT / root).resolve()
        return self.scope_root / "requests"

    @property
    def signature(self) -> str:
        return "|".join(
            (
                self.policy_version,
                self.mode,
                str(self.workdir),
                self.permission_profile,
                self.approval_policy,
                self.config_policy,
            )
        )

    def ensure_directories(self) -> None:
        if self.is_owner:
            return
        self.scope_root.mkdir(parents=True, exist_ok=True)
        (self.scope_root / "workspace").mkdir(exist_ok=True)
        (self.scope_root / "requests").mkdir(exist_ok=True)

    def apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply administrator-owned values after untrusted request construction."""
        result = dict(payload)
        result.pop("codex_sandbox", None)
        result.pop("codex_runtime_workspace_roots", None)
        result.update(
            {
                "codex_access_mode": self.mode,
                "codex_source_chat_name": self.chat_name,
                "codex_source_chat_type": "group" if self.is_group else "private",
                "codex_access_signature": self.signature,
                "codex_workdir": str(self.workdir),
                "codex_artifact_root": str(self.artifact_root),
                # Stable App Server sessions express unrestricted owner access
                # with the legacy sandbox field.  The `permissions` field is
                # experimental and is reserved for isolated `codex exec` runs.
                "codex_permission_profile": "" if self.is_owner else self.permission_profile,
                "codex_approval_policy": self.approval_policy,
                "codex_config_policy": self.config_policy,
                "codex_persistent_thread": self.persistent_thread,
            }
        )
        # runtimeWorkspaceRoots is an experimental App Server field.  Isolated
        # chats need the explicit boundary and run through `codex exec`; the
        # owner App Server profile is deliberately unrestricted and must not
        # receive this field on stable API sessions.
        if self.is_owner:
            result["codex_sandbox"] = "danger-full-access"
        else:
            result["codex_runtime_workspace_roots"] = [str(self.scope_root)]
        return result

    def public(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "scope_root": str(self.scope_root),
                "workdir": str(self.workdir),
                "artifact_root": str(self.artifact_root),
                "label": self.label,
                "scope_kind": self.scope_kind,
                "group_members_share_scope": bool(self.is_group and not self.is_owner),
                "skills_read_only": not self.is_owner,
                "local_command_network": "unrestricted" if self.is_owner else "disabled",
                "persistent_thread": self.persistent_thread,
            }
        )
        payload.pop("policy_version", None)
        return payload


class CodexAccessService:
    def __init__(self, *, scope_root: Optional[Path] = None) -> None:
        self.scope_root = Path(scope_root or _scope_base()).resolve()

    def for_chat(self, chat_name: str, *, ensure: bool = True) -> CodexAccessContext:
        normalized_name = str(chat_name or "").strip()
        is_group = False
        mode = ISOLATED_ACCESS
        db = SessionLocal()
        try:
            user = db.query(WeChatUser).filter(WeChatUser.chat_name == normalized_name).first()
            if user is not None:
                is_group = bool(user.is_group)
                mode = normalize_codex_access_mode(user.codex_access_mode)
        finally:
            db.close()

        # Fail closed for unknown chats, malformed database values, and groups.
        if is_group or mode != OWNER_FULL_ACCESS:
            mode = ISOLATED_ACCESS
        context = self._build(normalized_name, is_group=is_group, mode=mode)
        if ensure:
            context.ensure_directories()
        return context

    def for_user(self, user: WeChatUser, *, ensure: bool = False) -> CodexAccessContext:
        mode = normalize_codex_access_mode(user.codex_access_mode)
        is_group = bool(user.is_group)
        if is_group and mode == OWNER_FULL_ACCESS:
            mode = ISOLATED_ACCESS
        context = self._build(str(user.chat_name or ""), is_group=is_group, mode=mode)
        if ensure:
            context.ensure_directories()
        return context

    def _build(self, chat_name: str, *, is_group: bool, mode: str) -> CodexAccessContext:
        if mode == OWNER_FULL_ACCESS and not is_group:
            return CodexAccessContext(
                chat_name=chat_name,
                is_group=False,
                mode=OWNER_FULL_ACCESS,
                scope_root=PROJECT_ROOT.resolve(),
                workdir=PROJECT_ROOT.resolve(),
                permission_profile=OWNER_PERMISSION_PROFILE,
                approval_policy="on-request",
                config_policy="inherit",
                persistent_thread=True,
            )

        scope = chat_scope_path(chat_name, root=self.scope_root)
        return CodexAccessContext(
            chat_name=chat_name,
            is_group=is_group,
            mode=ISOLATED_ACCESS,
            scope_root=scope,
            workdir=scope,
            permission_profile=ISOLATED_PERMISSION_PROFILE,
            approval_policy="never",
            config_policy="isolated",
            persistent_thread=False,
        )


codex_access_service = CodexAccessService()
