"""
Dashboard API endpoints
提供 Dashboard 页面所需的统计数据
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import asyncio
import json
import os
import re
import logging
from pathlib import Path

from app.models.base import get_db
from app.models.user_permission import WeChatUser
from app.services.config_service import get_setting
from app.utils.dashboard_events import get_latest_dashboard_event, get_recent_dashboard_events

router = APIRouter()
logger = logging.getLogger(__name__)
_codex_refresh_lock = asyncio.Lock()
_codex_live_status_snapshot_file = Path("data/codex_usage_snapshot.json")
_codex_live_status_snapshot_version = 1


def _get_chat_logs_dir() -> Path:
    """获取聊天记录目录"""
    return Path(get_setting("CHAT_LOG_DIR", "data/chat_logs"))


def _count_today_messages() -> int:
    """统计今日消息总数"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return 0
        
        today = datetime.now().date()
        total = 0
        
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            # 字段名是 'time' 而不是 'timestamp'
                            time_str = entry.get('time', '')
                            if not time_str:
                                continue
                            # 解析时间 "2026-01-27 17:09:03"
                            timestamp = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                            if timestamp.date() == today:
                                total += 1
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception:
                continue
        
        return total
    except Exception:
        return 0


def _count_today_ai_replies() -> int:
    """统计今日 AI 回复数"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return 0
        
        today = datetime.now().date()
        total = 0
        bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
        
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            time_str = entry.get('time', '')
                            if not time_str:
                                continue
                            timestamp = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                            sender = entry.get('sender', '')
                            if timestamp.date() == today and sender == bot_name:
                                total += 1
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception:
                continue
        
        return total
    except Exception:
        return 0


def _count_active_users_today() -> int:
    """统计今日活跃用户数"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return 0
        
        today = datetime.now().date()
        active_users = set()
        bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
        
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            time_str = entry.get('time', '')
                            if not time_str:
                                continue
                            timestamp = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                            sender = entry.get('sender', '')
                            if timestamp.date() == today and sender != bot_name:
                                # 从文件名获取聊天名称
                                chat_name = log_file.stem
                                active_users.add(chat_name)
                                break  # 该用户已计数，跳到下一个文件
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception:
                continue
        
        return len(active_users)
    except Exception:
        return 0


def _get_recent_activities(limit: int = 20) -> List[Dict[str, Any]]:
    """获取最近的活动记录"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return []
        
        activities = []
        bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
        
        # 收集所有日志文件的最新条目
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                chat_name = log_file.stem
                with open(log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # 从后往前读取最近的几条
                    for line in reversed(lines[-50:]):  # 每个文件最多取50条
                        try:
                            entry = json.loads(line.strip())
                            time_str = entry.get('time', '')
                            sender = entry.get('sender', '')
                            content = entry.get('content', '')
                            
                            if not time_str:
                                continue
                            
                            # 判断是用户消息还是机器人回复
                            is_bot = sender == bot_name
                            
                            # 生成预览文本（简化处理，都当文本）
                            preview = content[:50] + '...' if len(content) > 50 else content
                            
                            activities.append({
                                'time': time_str,
                                'chat_name': chat_name,
                                'sender': sender,
                                'is_bot': is_bot,
                                'preview': preview
                            })
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
            except Exception:
                continue
        
        # 按时间排序，取最新的 limit 条
        activities.sort(key=lambda x: x['time'], reverse=True)
        return activities[:limit]
    except Exception:
        return []


def _get_top_users_today(limit: int = 5) -> List[Dict[str, Any]]:
    """获取今日最活跃用户"""
    try:
        logs_dir = _get_chat_logs_dir()
        if not logs_dir.exists():
            return []
        
        today = datetime.now().date()
        user_counts = {}
        bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
        
        for log_file in logs_dir.glob("*.jsonl"):
            try:
                chat_name = log_file.stem
                count = 0
                
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            time_str = entry.get('time', '')
                            if not time_str:
                                continue
                            timestamp = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                            sender = entry.get('sender', '')
                            if timestamp.date() == today and sender != bot_name:
                                count += 1
                        except (json.JSONDecodeError, ValueError, KeyError):
                            continue
                
                if count > 0:
                    user_counts[chat_name] = count
            except Exception:
                continue
        
        # 排序并获取 top N
        sorted_users = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        
        # 获取用户的 is_group 信息
        from app.models.base import SessionLocal
        db = SessionLocal()
        try:
            result = []
            for chat_name, count in sorted_users:
                user = db.query(WeChatUser).filter(WeChatUser.chat_name == chat_name).first()
                result.append({
                    'chat_name': chat_name,
                    'is_group': user.is_group if user else False,
                    'message_count': count
                })
            return result
        finally:
            db.close()
    except Exception:
        return []


def _get_codex_sessions_dir() -> Path:
    codex_home = os.getenv("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def _get_latest_codex_rollout() -> Optional[Path]:
    sessions_dir = _get_codex_sessions_dir()
    if not sessions_dir.exists():
        return None

    try:
        return max(
            sessions_dir.glob("**/rollout-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
        )
    except Exception:
        return None


def _timestamp_from_epoch(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    except Exception:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_rate_limit_window(limit: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(limit, dict):
        return None

    used = _number(limit.get("used_percent", limit.get("usedPercent")))
    if used is None:
        return None
    used = max(0.0, min(100.0, used))
    window = _number(limit.get("window_minutes", limit.get("windowDurationMins")))
    resets_at = _number(limit.get("resets_at", limit.get("resetsAt")))
    resets_at_value = int(resets_at) if resets_at is not None else None
    return {
        "used_percent": used,
        "remaining_percent": 100.0 - used,
        "window_minutes": int(window) if window is not None else None,
        "resets_at": resets_at_value,
        "resets_at_iso": _timestamp_from_epoch(resets_at_value),
    }


def _normalize_rate_limits(rate_limits: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "limit_id": rate_limits.get("limit_id", rate_limits.get("limitId")),
        "limit_name": rate_limits.get("limit_name", rate_limits.get("limitName")),
        "plan_type": rate_limits.get("plan_type", rate_limits.get("planType")),
        "rate_limit_reached_type": rate_limits.get(
            "rate_limit_reached_type", rate_limits.get("rateLimitReachedType")
        ),
        "credits": rate_limits.get("credits"),
        "individual_limit": rate_limits.get(
            "individual_limit", rate_limits.get("individualLimit")
        ),
    }
    for key in ("primary", "secondary"):
        normalized[key] = _normalize_rate_limit_window(rate_limits.get(key))
    return normalized


def _format_codex_quota(rate_limits: Dict[str, Any]) -> str:
    parts = []
    for key, label in (("primary", "Primary"), ("secondary", "Secondary")):
        limit = rate_limits.get(key) or {}
        remaining = limit.get("remaining_percent")
        window = limit.get("window_minutes")
        if isinstance(remaining, (int, float)):
            window_text = ""
            if isinstance(window, (int, float)):
                w_val = int(window)
                if w_val % 1440 == 0:
                    window_text = f" / {w_val // 1440}天"
                elif w_val % 60 == 0:
                    window_text = f" / {w_val // 60}小时"
                else:
                    window_text = f" / {w_val}分钟"
            parts.append(f"{label}: 剩余 {remaining:g}%{window_text}")
    return " | ".join(parts) if parts else "Codex rate limit data found"


def _usage_info_from_runtime(response: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = response.get("rateLimits")
    if not isinstance(snapshot, dict):
        return {
            "quota_available": False,
            "quota": None,
            "quota_message": "Codex runtime did not return account rate limits",
            "rate_limits": None,
            "rate_limits_by_limit_id": None,
            "rate_limit_updated_at": None,
            "rollout_file": None,
            "source": "app_server",
        }

    normalized = _normalize_rate_limits(snapshot)
    quota_available = any(
        isinstance((normalized.get(key) or {}).get("remaining_percent"), (int, float))
        for key in ("primary", "secondary")
    )
    buckets = response.get("rateLimitsByLimitId")
    normalized_buckets = None
    if isinstance(buckets, dict):
        normalized_buckets = {
            str(limit_id): _normalize_rate_limits(value)
            for limit_id, value in buckets.items()
            if isinstance(value, dict)
        }

    return {
        "quota_available": quota_available,
        "quota": _format_codex_quota(normalized) if quota_available else None,
        "quota_message": (
            "Read live account limits from Codex runtime"
            if quota_available
            else "Codex account has no percentage-based rate limit"
        ),
        "rate_limits": normalized,
        "rate_limits_by_limit_id": normalized_buckets,
        "rate_limit_reset_credits": response.get("rateLimitResetCredits"),
        "rate_limit_updated_at": datetime.now().isoformat(timespec="seconds"),
        "rollout_file": None,
        "source": "app_server",
    }


def _read_codex_live_status_snapshot(
    snapshot_file: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read the last successful Codex runtime result used by the dashboard."""
    path = snapshot_file or _codex_live_status_snapshot_file
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"读取 Codex 实时额度快照失败: {exc}")
        return None

    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("schema_version") != _codex_live_status_snapshot_version:
        return None

    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("usage_source") != "app_server" or not payload.get("quota_available"):
        return None
    if not isinstance(payload.get("rate_limits"), dict):
        return None
    return dict(payload)


def _write_codex_live_status_snapshot(
    payload: Dict[str, Any],
    snapshot_file: Optional[Path] = None,
) -> None:
    """Atomically persist a successful live result without credentials or tokens."""
    if payload.get("usage_source") != "app_server" or not payload.get("quota_available"):
        return

    path = snapshot_file or _codex_live_status_snapshot_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    snapshot = {
        "schema_version": _codex_live_status_snapshot_version,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    }
    try:
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _read_codex_rate_limits_from_rollout(rollout_file: Optional[Path] = None) -> Dict[str, Any]:
    path = rollout_file or _get_latest_codex_rollout()
    if not path:
        return {
            "quota_available": False,
            "quota": None,
            "quota_message": f"No rollout files found under {_get_codex_sessions_dir()}",
            "rollout_file": None,
            "rate_limits": None,
            "source": "rollout",
            "rate_limit_updated_at": None,
            "session": None,
        }

    session_meta: Dict[str, Any] = {}
    latest_rate_limits: Optional[Dict[str, Any]] = None
    latest_timestamp = None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in lines:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                session_meta = event["payload"]
                break

        for line in reversed(lines):
            try:
                event = json.loads(line)
            except Exception:
                continue
            payload = event.get("payload") or {}
            rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
            if isinstance(rate_limits, dict):
                latest_rate_limits = _normalize_rate_limits(rate_limits)
                latest_timestamp = event.get("timestamp")
                break
    except Exception as exc:
        return {
            "quota_available": False,
            "quota": None,
            "quota_message": f"Failed to read rollout file: {exc}",
            "rollout_file": str(path),
            "rate_limits": None,
            "source": "rollout",
            "rate_limit_updated_at": None,
            "session": session_meta or None,
        }

    if not latest_rate_limits:
        return {
            "quota_available": False,
            "quota": None,
            "quota_message": "Latest rollout file does not contain rate_limits yet",
            "rollout_file": str(path),
            "rate_limits": None,
            "source": "rollout",
            "rate_limit_updated_at": None,
            "session": session_meta or None,
        }

    return {
        "quota_available": True,
        "quota": _format_codex_quota(latest_rate_limits),
        "quota_message": "Read from latest Codex rollout rate_limits",
        "rollout_file": str(path),
        "rate_limits": latest_rate_limits,
        "source": "rollout",
        "rate_limit_updated_at": latest_timestamp,
        "session": session_meta or None,
    }


async def _get_codex_status_payload(refresh: bool = False) -> Dict[str, Any]:
    now = datetime.now()
    errors: List[str] = []
    initialize_result: Dict[str, Any] = {}
    refresh_succeeded = False
    if not refresh:
        live_snapshot = _read_codex_live_status_snapshot()
        if live_snapshot:
            live_snapshot.update({
                "refreshed": False,
                "refresh_succeeded": None,
                "served_from_snapshot": True,
            })
            return live_snapshot

    if refresh:
        async with _codex_refresh_lock:
            try:
                from app.services.agent_runtime import get_agent_runtime

                timeout = int(os.getenv("CODEX_USAGE_REFRESH_TIMEOUT", "30"))
                response, runtime_worker = await asyncio.to_thread(
                    get_agent_runtime().read_rate_limits,
                    timeout,
                )
                initialize_result = {
                    "userAgent": runtime_worker.get("user_agent"),
                    "codexVersion": runtime_worker.get("codex_version"),
                }
                usage_info = _usage_info_from_runtime(response)
                refresh_succeeded = bool(usage_info.get("quota_available"))
            except Exception as exc:
                errors.append(f"Codex live usage refresh failed: {exc}")
                live_snapshot = _read_codex_live_status_snapshot()
                if live_snapshot:
                    live_snapshot.update({
                        "status": "warning",
                        "login_status": "Live refresh failed; showing last successful live usage",
                        "quota_message": "Live refresh failed; showing last successful live usage",
                        "refreshed": True,
                        "refresh_succeeded": False,
                        "served_from_snapshot": True,
                        "errors": errors,
                    })
                    return live_snapshot
                usage_info = _read_codex_rate_limits_from_rollout()
                if usage_info.get("quota_available"):
                    usage_info["quota_message"] = "Live refresh failed; showing cached rollout data"
    else:
        usage_info = _read_codex_rate_limits_from_rollout()

    session = usage_info.get("session") or {}
    logged_in = bool(usage_info.get("quota_available"))
    user_agent = initialize_result.get("userAgent") or ""
    version_match = re.match(r"[^/]+/([^\s]+)", user_agent)
    version = (
        session.get("cli_version")
        or initialize_result.get("codexVersion")
        or (version_match.group(1) if version_match else "")
    )
    model = session.get("model") or os.getenv("CODEX_PROXY_MODEL", "gpt-5.6-sol")
    rate_limits = usage_info.get("rate_limits") or {}
    plan_type = rate_limits.get("plan_type")

    data = {
        "status": "ok" if logged_in and (not refresh or refresh_succeeded) else "warning",
        "logged_in": logged_in,
        "login_status": usage_info.get("quota_message"),
        "version": version,
        "model": model,
        "model_provider": session.get("model_provider") or "openai",
        "auth_mode": "chatgpt" if plan_type else ("codex session" if logged_in else "-"),
        "plan_type": plan_type,
        "quota_available": usage_info.get("quota_available"),
        "quota": usage_info.get("quota"),
        "quota_message": usage_info.get("quota_message"),
        "rate_limits": rate_limits or None,
        "rate_limits_by_limit_id": usage_info.get("rate_limits_by_limit_id"),
        "rate_limit_reset_credits": usage_info.get("rate_limit_reset_credits"),
        "rate_limit_updated_at": usage_info.get("rate_limit_updated_at"),
        "rollout_file": usage_info.get("rollout_file"),
        "refreshed": refresh,
        "refresh_succeeded": refresh_succeeded if refresh else None,
        "served_from_snapshot": False,
        "usage_source": usage_info.get("source"),
        "updated_at": now.isoformat(timespec="seconds"),
        "errors": errors,
    }
    if refresh_succeeded:
        try:
            _write_codex_live_status_snapshot(data)
        except Exception as exc:
            logger.warning(f"保存 Codex 实时额度快照失败: {exc}")
    return data


@router.get("/stats")
def get_dashboard_stats():
    """获取 Dashboard 核心统计数据"""
    try:
        from app.services.llm_manager import get_llm_manager
        import psutil
        
        # LLM Stats Aggregation
        llm_manager = get_llm_manager()
        llm_stats = llm_manager.get_stats()
        session_stats = llm_stats.get('session', {})
        
        token_usage = 0
        total_calls = 0
        error_count = 0
        llm_response_times = []
        
        e2e_latency_stats = session_stats.get('assistant.reply_latency')
        
        for key, data in session_stats.items():
            # Skip the virtual latency metric for call counts and tokens
            if key == 'assistant.reply_latency':
                continue
                
            token_usage += data.get('total_tokens', 0)
            total_calls += data.get('count', 0)
            error_count += data.get('error_count', 0)
            llm_response_times.extend(data.get('response_times', []))
            
        avg_latency = 0.0
        
        # Prioritize E2E Latency if available
        if e2e_latency_stats and e2e_latency_stats.get('response_times'):
            times = e2e_latency_stats.get('response_times', [])
            if times:
                avg_latency = sum(times) / len(times)
        # Fallback to LLM latency if E2E not available
        elif llm_response_times:
            avg_latency = sum(llm_response_times) / len(llm_response_times)
            
        # Runtime Duration
        create_time = psutil.Process().create_time()
        uptime_seconds = datetime.now().timestamp() - create_time
        # Format as HH:MM:SS
        hours, remainder = divmod(int(uptime_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_duration = f"{hours}h {minutes}m {seconds}s"
        
    except Exception as e:
        logger.error(f"Error calculating stats: {e}")
        token_usage = 0
        total_calls = 0
        error_count = 0
        avg_latency = 0.0
        runtime_duration = "0h 0m 0s"
    
    return {
        "today_messages": _count_today_messages(),
        "today_ai_replies": _count_today_ai_replies(),
        "active_users": _count_active_users_today(),
        "token_usage": token_usage,
        "runtime_stats": {
            "total_calls": total_calls,
            "avg_latency": round(avg_latency, 2),
            "error_count": error_count,
            "duration": runtime_duration
        }
    }


@router.get("/codex-status")
async def get_codex_status():
    """读取最近一次成功的实时额度快照；没有快照时回退到 rollout。"""
    try:
        return await _get_codex_status_payload(refresh=False)
    except Exception as exc:
        logger.warning(f"获取 Codex 状态失败: {exc}")
        return {
            "status": "error",
            "logged_in": False,
            "login_status": "Codex status unavailable",
            "version": "",
            "model": os.getenv("CODEX_PROXY_MODEL", "gpt-5.6-sol"),
            "model_provider": "openai",
            "quota_available": False,
            "quota": None,
            "quota_message": f"获取失败: {exc}",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "errors": [str(exc)],
        }


@router.post("/codex-status/refresh")
async def refresh_codex_status():
    """通过 Codex 运行时读取实时账户额度，不启动模型会话。"""
    try:
        return await _get_codex_status_payload(refresh=True)
    except Exception as exc:
        logger.warning(f"刷新 Codex 状态失败: {exc}")
        return {
            "status": "error",
            "logged_in": False,
            "login_status": "Codex status unavailable",
            "version": "",
            "model": os.getenv("CODEX_PROXY_MODEL", "gpt-5.6-sol"),
            "model_provider": "openai",
            "quota_available": False,
            "quota": None,
            "quota_message": f"刷新失败: {exc}",
            "rate_limits": None,
            "rate_limit_updated_at": None,
            "rollout_file": None,
            "refreshed": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "errors": [str(exc)],
        }


@router.get("/recent-activities")
def get_recent_activities(limit: int = 20):
    """获取最近的活动记录"""
    return {
        "activities": _get_recent_activities(limit)
    }


@router.get("/top-users")
def get_top_users(limit: int = 5):
    """获取今日最活跃用户排行"""
    return {
        "users": _get_top_users_today(limit)
    }


@router.get("/top-plugins")
def get_top_plugins(limit: int = 5):
    """获取今日最热门插件排行"""
    # 这个需要从插件调用日志中统计
    # 暂时返回空数据，后续可以通过日志分析实现
    return {
        "plugins": []
    }


@router.get("/latest-judge")
async def get_latest_judge():
    """获取最新的 judge 输出"""
    try:
        # 优先读取结构化事件（不依赖日志文案）
        judge_events = get_recent_dashboard_events("judge_decision", limit=10)
        if judge_events:
            history = []
            for event in judge_events:
                payload = event.get("payload", {}) or {}
                history.append({
                    "judge_output": {
                        "should_reply": payload.get("should_reply"),
                        "reason": payload.get("reason"),
                        "judge_name": payload.get("judge_name"),
                    },
                    "timestamp": event.get("timestamp"),
                    "should_reply": payload.get("should_reply"),
                    "reason": payload.get("reason"),
                    "judge_name": payload.get("judge_name"),
                    "role_name": payload.get("role_name"),
                    "atmosphere": payload.get("atmosphere"),
                })

            payload = judge_events[0].get("payload", {}) or {}
            should_reply = payload.get("should_reply")
            reason = payload.get("reason")
            judge_name = payload.get("judge_name")
            return {
                "judge_output": {
                    "should_reply": should_reply,
                    "reason": reason,
                    "judge_name": judge_name,
                },
                "timestamp": judge_events[0].get("timestamp"),
                "should_reply": should_reply,
                "reason": reason,
                "judge_name": judge_name,
                "history": history,
            }

        # 兼容旧版本：回退到日志解析
        # 读取应用日志文件
        log_file = Path("logs/app.log")
        if not log_file.exists():
            return {
                "judge_output": None,
                "timestamp": None,
                "should_reply": None,
                "reason": "日志文件不存在",
                "judge_name": None,
                "history": [],
            }
        
        # 读取最后 2000 行日志（避免读取整个文件）
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            recent_lines = lines[-2000:] if len(lines) > 2000 else lines
        
        # 查找最新的 judge 输出
        # 日志格式: 2026-01-29 10:32:50,163 [INFO] app.assistant.handler: ⚖️ Judge decided to STAY SILENT: reason
        # 或: ⚖️ Judge decided to REPLY: reason
        judge_pattern = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Judge decided to (STAY SILENT|REPLY): (.+)')
        
        latest_judge = None
        latest_timestamp = None
        should_reply = None
        reason = None
        judge_name = None
        history = []
        
        # 从后往前查找（最新的在最后）
        for line in reversed(recent_lines):
            if 'Judge decided to' in line:
                match = judge_pattern.search(line)
                if match:
                    latest_timestamp = match.group(1)
                    decision = match.group(2)  # "STAY SILENT" or "REPLY"
                    reason = match.group(3).strip()
                    should_reply = (decision == "REPLY")
                    
                    item = {
                        "should_reply": should_reply,
                        "reason": reason,
                        "judge_name": None,
                    }
                    if latest_judge is None:
                        latest_judge = item
                    history.append({
                        "judge_output": item,
                        "timestamp": latest_timestamp,
                        "should_reply": should_reply,
                        "reason": reason,
                        "judge_name": None,
                    })
                    if len(history) >= 10:
                        break
        
        if latest_judge:
            latest_item = history[0] if history else {}
            return {
                "judge_output": latest_judge,
                "timestamp": latest_item.get("timestamp", latest_timestamp),
                "should_reply": latest_item.get("should_reply"),
                "reason": latest_item.get("reason"),
                "judge_name": judge_name,
                "history": history,
            }
        else:
            return {
                "judge_output": None,
                "timestamp": None,
                "should_reply": None,
                "reason": "暂无 Judge 输出",
                "judge_name": None,
                "history": [],
            }
            
    except Exception as e:
        logger.error(f"获取最新 judge 输出失败: {e}")
        return {
            "judge_output": None,
            "timestamp": None,
            "should_reply": None,
            "reason": f"获取失败: {str(e)}",
            "judge_name": None,
            "history": [],
        }


@router.get("/latest-search")
async def get_latest_search():
    """获取最新的 web search 输出"""
    try:
        # 优先读取结构化事件（不依赖日志文案）
        search_event = get_latest_dashboard_event("web_search")
        if search_event:
            payload = search_event.get("payload", {}) or {}
            query = payload.get("query")
            content = payload.get("content")
            result_length = payload.get("result_length")
            model_name = payload.get("model_name")
            return {
                "search_output": {
                    "query": query,
                    "content": content,
                    "result_length": result_length,
                    "model_name": model_name
                },
                "timestamp": search_event.get("timestamp"),
                "query": query,
                "content": content,
                "result_length": result_length,
                "model_name": model_name
            }

        # 兼容旧版本：回退到日志解析
        # 读取应用日志文件
        log_file = Path("logs/app.log")
        if not log_file.exists():
            return {
                "search_output": None,
                "timestamp": None,
                "query": None,
                "content": None,
                "result_length": None,
                "reason": "日志文件不存在"
            }
        
        # 读取最后 2000 行日志（避免读取整个文件）
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            recent_lines = lines[-2000:] if len(lines) > 2000 else lines
        
        # 查找最新的 search 输出
        # 新格式: 🔍 Web Search Success | Query: "query" | Length: 1234 | Content: actual content...
        # 旧格式: 🔍 Web Search Success | Query: "query" | Results: 1234 chars
        search_pattern_new = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*🔍 Web Search Success \| Query: "(.+?)" \| Length: (\d+) \| Content: (.+)')
        search_pattern_old = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*🔍 Web Search Success \| Query: "(.+?)" \| Results: (\d+) chars')
        
        latest_search = None
        latest_timestamp = None
        query = None
        content = None
        result_length = None
        model_name = None
        
        # 从后往前查找（最新的在最后）
        for i in range(len(recent_lines) - 1, -1, -1):
            line = recent_lines[i]
            if '🔍 Web Search Success' in line:
                # 先尝试新格式（带 Content）
                if '| Content: ' in line:
                    # 提取基本信息（支持含 Model 字段的新格式）
                    match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Query: "(.+?)" \| (?:Model: ([^|]+) \| )?Length: (\d+)', line)
                    if match:
                        latest_timestamp = match.group(1)
                        query = match.group(2).strip()
                        model_name = match.group(3).strip() if match.group(3) else None
                        result_length = int(match.group(4))
                        
                        # 提取内容（从 "Content: " 开始）
                        content_start = line.find('| Content: ') + len('| Content: ')
                        content_parts = [line[content_start:].rstrip('\n')]
                        
                        # 继续读取后续行，直到遇到新的日志条目或达到限制
                        j = i + 1
                        max_lines = 200  # 最多读取200行（支持更长的搜索结果）
                        while j < len(recent_lines) and j < i + max_lines:
                            next_line = recent_lines[j]
                            # 如果遇到新的日志条目（以时间戳开头），停止
                            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', next_line):
                                break
                            content_parts.append(next_line.rstrip('\n'))
                            j += 1
                        
                        # 合并内容（不限制长度，在前端用 modal 显示）
                        content = '\n'.join(content_parts).strip()
                        
                        latest_search = {
                            "query": query,
                            "content": content,
                            "result_length": result_length,
                            "model_name": model_name
                        }
                        break
                
                # 如果新格式不匹配，尝试旧格式（无 Content）
                match = search_pattern_old.search(line)
                if match:
                    latest_timestamp = match.group(1)
                    query = match.group(2).strip()
                    result_length = int(match.group(3))
                    content = "（旧版本日志，无内容预览）"
                    
                    latest_search = {
                        "query": query,
                        "content": content,
                        "result_length": result_length
                    }
                    break
        
        if latest_search:
            return {
                "search_output": latest_search,
                "timestamp": latest_timestamp,
                "query": query,
                "content": content,
                "result_length": result_length,
                "model_name": model_name
            }
        else:
            return {
                "search_output": None,
                "timestamp": None,
                "query": None,
                "content": None,
                "result_length": None,
                "reason": "暂无搜索记录"
            }
            
    except Exception as e:
        logger.error(f"获取最新搜索输出失败: {e}")
        return {
            "search_output": None,
            "timestamp": None,
            "query": None,
            "content": None,
            "result_length": None,
            "reason": f"获取失败: {str(e)}"
        }
