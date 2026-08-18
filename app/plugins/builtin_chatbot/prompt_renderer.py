"""
Prompt 渲染工具
- 安全变量替换（仅替换已知变量，避免 JSON 花括号误伤）
- Judge 双模式渲染（simple/template）
"""

import re
from typing import Dict, Iterable


ROLE_ALLOWED_VARS = ("chat_text", "search_results", "sender", "content")
JUDGE_ALLOWED_VARS = ("chat_text",)


def _safe_replace(template: str, variables: Dict[str, str], allowed_keys: Iterable[str]) -> str:
    """仅替换白名单变量，兼容 {var} 与 {{var}} 两种写法。"""
    if not template:
        return ""

    rendered = template
    for key in allowed_keys:
        value = str(variables.get(key, ""))
        # 支持 {{ key }}
        rendered = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", lambda _: value, rendered)
        # 支持 { key }
        rendered = re.sub(r"\{\s*" + re.escape(key) + r"\s*\}", lambda _: value, rendered)
    return rendered


def render_role_prompt(template: str, variables: Dict[str, str]) -> str:
    """渲染 Role Prompt（安全替换模式）。"""
    safe_vars = {
        "chat_text": "",
        "search_results": "",
        "sender": "",
        "content": "",
    }
    safe_vars.update(variables or {})
    return _safe_replace(template, safe_vars, ROLE_ALLOWED_VARS)


def render_judge_prompt(template: str, mode: str, variables: Dict[str, str]) -> str:
    """渲染 Judge Prompt（simple/template 双模式）。"""
    safe_vars = {"chat_text": ""}
    safe_vars.update(variables or {})
    chat_text = str(safe_vars.get("chat_text", ""))

    if (mode or "simple").lower() == "template":
        return _safe_replace(template, safe_vars, JUDGE_ALLOWED_VARS)

    # simple 模式：不要求用户写变量，系统统一拼装
    rules = (template or "").strip() or "根据上下文判断是否应该主动回复。"
    return (
        "## Role\n"
        "你是一个群聊主动回复决策器，只负责判断“应不应该回复”。\n\n"
        "## Judge Rules\n"
        f"{rules}\n\n"
        "## Context\n"
        "[对话开始]\n"
        f"{chat_text}\n"
        "[对话结束]\n\n"
        "## Output (Strict JSON)\n"
        "{\n"
        '  "atmosphere": "简述当前氛围",\n'
        '  "should_reply": true/false,\n'
        '  "reason": "为什么判断需要或不需要回复"\n'
        "}"
    )
