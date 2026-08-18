"""Aggregated Chatbot console data and chat-level configuration mutations."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.chatbot_judge import ChatBotJudge, UserChatBotJudge
from app.models.chatbot_role import ChatBotRole, UserChatBotRole
from app.models.user_permission import UserPermission, WeChatUser
from app.services.capability_service import CapabilityService
from app.services.config_service import get_setting
from app.utils.bot_mentions import bot_names_for_user
from app.utils.plugin_config import get_plugin_setting


class AssistantConsoleError(ValueError):
    pass


def _json_object(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            parsed = str(value).splitlines()
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in parsed if str(item or "").strip()))


def _memory_summary(
    permission: Optional[UserPermission],
    global_config: Mapping[str, Any],
) -> Dict[str, Any]:
    profile = _json_object(permission.memory_profile if permission else None)
    raw_overrides = profile.get("overrides")
    overrides = raw_overrides if isinstance(raw_overrides, dict) else {
        key: value
        for key, value in profile.items()
        if str(key).startswith("memory_")
    }
    overrides = {
        key: value
        for key, value in overrides.items()
        if key in global_config
    }
    if not bool(profile.get("enabled")):
        mode = "inherit"
        effective_enabled = bool(global_config.get("memory_enabled", True))
    elif overrides.get("memory_enabled") is False:
        mode = "off"
        effective_enabled = False
    else:
        mode = "custom"
        effective_enabled = bool(overrides.get("memory_enabled", True))
    return {
        "mode": mode,
        "source": "global" if mode == "inherit" else "chat",
        "effective_enabled": effective_enabled,
        "overrides": overrides,
    }


class AssistantConsoleService:
    def __init__(self, plugin_manager: Any, wechat_manager: Any, db: Session):
        self.plugin_manager = plugin_manager
        self.wechat_manager = wechat_manager
        self.db = db

    def _active_chat_names(self) -> set[str]:
        if not self.wechat_manager:
            return set()
        try:
            active = self.wechat_manager.get_listened_chats()
        except Exception:
            return set()
        if isinstance(active, dict):
            return set(active)
        if isinstance(active, list):
            return set(active)
        return set()

    def _roles(self) -> tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        bindings = self.db.query(UserChatBotRole).all()
        counts: Dict[int, int] = {}
        for binding in bindings:
            counts[binding.role_id] = counts.get(binding.role_id, 0) + 1
        items = []
        by_id = {}
        for role in self.db.query(ChatBotRole).order_by(ChatBotRole.id).all():
            item = {
                "id": role.id,
                "name": role.name,
                "display_name": role.display_name,
                "description": role.description or "",
                "prompt": role.prompt,
                "output_split_enabled": bool(role.output_split_enabled),
                "output_max_chars": role.output_max_chars,
                "output_max_count": role.output_max_count,
                "output_strip_trailing_period": bool(role.output_strip_trailing_period),
                "output_interval_seconds": role.output_interval_seconds,
                "is_builtin": str(role.is_builtin or "false").lower() == "true",
                "user_count": counts.get(role.id, 0),
            }
            items.append(item)
            by_id[role.id] = item
        return items, by_id

    def _judges(self) -> tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        bindings = self.db.query(UserChatBotJudge).all()
        counts: Dict[int, int] = {}
        for binding in bindings:
            counts[binding.judge_id] = counts.get(binding.judge_id, 0) + 1
        items = []
        by_id = {}
        for judge in self.db.query(ChatBotJudge).order_by(ChatBotJudge.id).all():
            item = {
                "id": judge.id,
                "name": judge.name,
                "display_name": judge.display_name,
                "description": judge.description or "",
                "prompt": judge.prompt,
                "prompt_mode": judge.prompt_mode or "simple",
                "trigger_msg_threshold": judge.trigger_msg_threshold,
                "trigger_interval_minutes": judge.trigger_interval_minutes,
                "cooldown_msg_threshold": judge.cooldown_msg_threshold,
                "cooldown_minutes": judge.cooldown_minutes,
                "is_builtin": str(judge.is_builtin or "false").lower() == "true",
                "user_count": counts.get(judge.id, 0),
            }
            items.append(item)
            by_id[judge.id] = item
        return items, by_id

    def _model_summary(self) -> Dict[str, Any]:
        try:
            from app.services.llm_manager import get_llm_manager

            manager = get_llm_manager()
            with manager._config_lock:
                model_configs = copy.deepcopy(manager.config.get("models", {}))
                mappings = copy.deepcopy(
                    manager.config.get("plugin_mappings", {}).get("builtin_chatbot", {})
                )
        except Exception:
            return {"models": [], "mappings": {}}

        models = [
            {
                "id": model_id,
                "model": config.get("model") or model_id,
                "provider": config.get("custom_llm_provider") or config.get("provider") or "",
            }
            for model_id, config in model_configs.items()
        ]
        public_mappings = {
            call_type: {
                "primary": mapping.get("primary") or "",
                "fallback": list(mapping.get("fallback") or []),
            }
            for call_type, mapping in mappings.items()
        }
        return {"models": models, "mappings": public_mappings}

    def overview(self) -> Dict[str, Any]:
        roles, roles_by_id = self._roles()
        judges, judges_by_id = self._judges()
        role_bindings = {
            binding.user_id: binding.role_id
            for binding in self.db.query(UserChatBotRole).all()
        }
        judge_bindings = {
            binding.user_id: binding.judge_id
            for binding in self.db.query(UserChatBotJudge).all()
        }
        active_names = self._active_chat_names()
        default_role_name = str(
            get_plugin_setting("builtin_chatbot", "default_role", "default") or "default"
        )
        default_role = next((role for role in roles if role["name"] == default_role_name), None)
        global_bot_name = str(get_setting("WECHAT_BOT_NAME", "微信助手") or "微信助手")
        from app.services.memory_console_service import MemoryConsoleService

        global_memory_config = MemoryConsoleService.global_memory_config()

        users = (
            self.db.query(WeChatUser)
            .options(selectinload(WeChatUser.permissions))
            .order_by(WeChatUser.id)
            .all()
        )
        chats = []
        for user in users:
            permission = next(
                (item for item in user.permissions if item.plugin_name == "builtin_chatbot"),
                None,
            )
            role_id = role_bindings.get(user.id)
            judge_id = judge_bindings.get(user.id)
            profile = _json_object(permission.memory_profile if permission else None)
            memory_override_enabled = bool(profile.get("enabled"))
            bot_names = bot_names_for_user(user, global_bot_name)
            chats.append(
                {
                    "id": user.id,
                    "chat_name": user.chat_name,
                    "remark": user.remark or "",
                    "is_group": bool(user.is_group),
                    "is_listening": user.chat_name in active_names,
                    "enabled": permission is not None,
                    "proactive_enabled": bool(permission.proactive_enabled) if permission else False,
                    "followup_enabled": bool(permission.followup_enabled) if permission else False,
                    "followup_window_seconds": int(permission.followup_window_seconds or 60) if permission else 60,
                    "followup_merge_seconds": int(permission.followup_merge_seconds or 3) if permission else 3,
                    "followup_max_turns": int(permission.followup_max_turns or 3) if permission else 3,
                    "ignored_senders": _json_list(permission.ignored_senders if permission else None),
                    "memory_override_enabled": memory_override_enabled,
                    "memory": _memory_summary(permission, global_memory_config),
                    "role": roles_by_id.get(role_id) or default_role,
                    "role_source": "chat" if role_id else "global",
                    "judge": judges_by_id.get(judge_id),
                    "bot_group_nickname": user.bot_group_nickname or "",
                    "bot_group_nickname_auto_enabled": bool(user.bot_group_nickname_auto_enabled),
                    "bot_group_nickname_detected": user.bot_group_nickname_detected or "",
                    "bot_group_nickname_checked_at": user.bot_group_nickname_checked_at or "",
                    "bot_group_nickname_effective": bot_names[0] if bot_names else global_bot_name,
                }
            )

        capability_service = CapabilityService(self.plugin_manager, self.db)
        capability = capability_service.get_capability("builtin_chatbot")
        global_flags = {
            "default_role": default_role_name,
            "allow_mention_trigger": bool(get_plugin_setting("builtin_chatbot", "allow_mention_trigger", True)),
            "memory_enabled": bool(get_plugin_setting("builtin_chatbot", "memory_enabled", True)),
            "search_enabled": bool(get_plugin_setting("builtin_chatbot", "search_enabled", True)),
            "ocr_enabled": bool(get_plugin_setting("builtin_chatbot", "ocr_enabled", False)),
            "memory": global_memory_config,
        }
        enabled_chats = sum(1 for chat in chats if chat["enabled"])
        return {
            "capability": capability,
            "summary": {
                "chat_count": len(chats),
                "enabled_chat_count": enabled_chats,
                "proactive_chat_count": sum(1 for chat in chats if chat["enabled"] and chat["proactive_enabled"]),
                "role_count": len(roles),
                "judge_count": len(judges),
            },
            "global": global_flags,
            "models": self._model_summary(),
            "roles": roles,
            "judges": judges,
            "chats": chats,
        }

    def update_chat(self, user_id: int, changes: Mapping[str, Any]) -> None:
        user = self.db.query(WeChatUser).filter(WeChatUser.id == user_id).first()
        if user is None:
            raise AssistantConsoleError("聊天不存在")

        changes = dict(changes)
        memory_mode = changes.get("memory_mode")
        if memory_mode is not None and memory_mode not in {"inherit", "off", "custom"}:
            raise AssistantConsoleError("不支持的记忆模式")
        nickname_fields = {"bot_group_nickname", "bot_group_nickname_auto_enabled"}
        if nickname_fields.intersection(changes) and not user.is_group:
            raise AssistantConsoleError("群内机器人昵称只能配置于群聊")
        if "bot_group_nickname" in changes:
            nickname = str(changes.get("bot_group_nickname") or "").strip().lstrip("@").strip()
            if "\n" in nickname or "\r" in nickname:
                raise AssistantConsoleError("群内机器人昵称不能换行")
            user.bot_group_nickname = nickname or None
        if "bot_group_nickname_auto_enabled" in changes:
            user.bot_group_nickname_auto_enabled = bool(changes["bot_group_nickname_auto_enabled"])
        if changes.get("enabled") is not False and not user.is_group and changes.get("proactive_enabled") is True:
            raise AssistantConsoleError("私聊收到消息时会直接回复，不支持主动回复开关")

        # 主动回复只有一个用户入口：聊天级开关。开启时必须有 Judge；
        # 若界面没有显式选择，则沿用已有绑定或自动绑定默认 Judge。
        if (
            changes.get("enabled") is not False
            and user.is_group
            and changes.get("proactive_enabled") is True
            and changes.get("judge_id") is None
        ):
            current_binding = (
                self.db.query(UserChatBotJudge)
                .filter(UserChatBotJudge.user_id == user_id)
                .first()
            )
            if current_binding:
                changes["judge_id"] = current_binding.judge_id
            else:
                default_judge = (
                    self.db.query(ChatBotJudge)
                    .filter(ChatBotJudge.name == "default_judge")
                    .first()
                    or self.db.query(ChatBotJudge).order_by(ChatBotJudge.id).first()
                )
                if default_judge is None:
                    raise AssistantConsoleError("启用主动回复前需要先创建 Judge")
                changes["judge_id"] = default_judge.id

        # Validate referenced records before changing the current unit of work.
        # This keeps an invalid role/judge request from leaving pending permission
        # mutations in a request-scoped SQLAlchemy session.
        if changes.get("role_id") is not None and not self.db.query(ChatBotRole).filter(
            ChatBotRole.id == changes["role_id"]
        ).first():
            raise AssistantConsoleError("角色不存在")
        if changes.get("judge_id") is not None and not self.db.query(ChatBotJudge).filter(
            ChatBotJudge.id == changes["judge_id"]
        ).first():
            raise AssistantConsoleError("Judge 不存在")

        permission = (
            self.db.query(UserPermission)
            .filter(
                UserPermission.user_id == user_id,
                UserPermission.plugin_name == "builtin_chatbot",
            )
            .first()
        )
        permission_fields = {
            "proactive_enabled",
            "followup_enabled",
            "followup_window_seconds",
            "followup_merge_seconds",
            "followup_max_turns",
        }
        has_permission_changes = bool(
            permission_fields.intersection(changes) or "ignored_senders" in changes
        )
        explicitly_disabled = changes.get("enabled") is False
        should_enable = bool(
            changes.get("enabled") is True
            or ("enabled" not in changes and (permission is not None or has_permission_changes))
        )
        if (
            permission is None
            and memory_mode in {"off", "custom"}
            and changes.get("enabled") is not True
        ):
            raise AssistantConsoleError("请先为该聊天启用 AI 助手，再设置独立记忆模式")
        if should_enable and permission is None:
            permission = UserPermission(user_id=user_id, plugin_name="builtin_chatbot")
            self.db.add(permission)
        if explicitly_disabled and permission is not None:
            self.db.delete(permission)
            permission = None
        elif permission is not None:
            for field_name in permission_fields.intersection(changes):
                setattr(permission, field_name, changes[field_name])
            if "ignored_senders" in changes:
                permission.ignored_senders = (
                    json.dumps(_json_list(changes["ignored_senders"]), ensure_ascii=False)
                    if changes["ignored_senders"]
                    else None
                )
            if memory_mode is not None:
                if memory_mode == "inherit":
                    permission.memory_profile = None
                elif memory_mode == "off":
                    permission.memory_profile = json.dumps(
                        {
                            "enabled": True,
                            "overrides": {"memory_enabled": False},
                        },
                        ensure_ascii=False,
                    )
                else:
                    from app.services.memory_console_service import MemoryConsoleService

                    requested = changes.get("memory_overrides")
                    if not isinstance(requested, dict):
                        requested = _json_object(permission.memory_profile).get(
                            "overrides",
                            {},
                        )
                    try:
                        overrides = MemoryConsoleService.validate_memory_overrides(
                            requested
                        )
                    except ValueError as exc:
                        raise AssistantConsoleError(str(exc)) from exc
                    overrides["memory_enabled"] = True
                    permission.memory_profile = json.dumps(
                        {"enabled": True, "overrides": overrides},
                        ensure_ascii=False,
                    )

        reload_roles = False
        if "role_id" in changes:
            role_id = changes["role_id"]
            binding = self.db.query(UserChatBotRole).filter(UserChatBotRole.user_id == user_id).first()
            if role_id is None:
                if binding:
                    self.db.delete(binding)
            else:
                if binding:
                    binding.role_id = role_id
                else:
                    self.db.add(UserChatBotRole(user_id=user_id, role_id=role_id))
            reload_roles = True

        reload_judges = False
        if "judge_id" in changes:
            judge_id = changes["judge_id"]
            binding = self.db.query(UserChatBotJudge).filter(UserChatBotJudge.user_id == user_id).first()
            if judge_id is None:
                if binding:
                    self.db.delete(binding)
            else:
                if binding:
                    binding.judge_id = judge_id
                else:
                    self.db.add(UserChatBotJudge(user_id=user_id, judge_id=judge_id))
            reload_judges = True

        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        if reload_roles or reload_judges:
            try:
                from app.plugins.builtin_chatbot.main import get_chatbot_plugin

                plugin = get_chatbot_plugin()
                if plugin and reload_roles:
                    plugin.reload_roles()
                if plugin and reload_judges:
                    plugin.reload_judges()
            except Exception:
                # The database is authoritative; a future request or plugin
                # reload will refresh the in-memory cache.
                pass
        if memory_mode is not None:
            try:
                from app.plugins.builtin_chatbot.main import get_chatbot_plugin

                plugin = get_chatbot_plugin()
                if plugin and hasattr(plugin, "invalidate_memory_context"):
                    plugin.invalidate_memory_context(user.chat_name)
            except Exception:
                pass
