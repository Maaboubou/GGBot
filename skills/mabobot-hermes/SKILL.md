---
name: mabobot-hermes
description: Operate, inspect, and recover a local Mabobot deployment that exposes FastAPI and wx_bot HTTP interfaces for an already logged-in Windows WeChat client. Use when Hermes or Codex needs to send WeChat messages, manage listened chats, inspect plugin state, read logs, reload plugin config, or recover from Mabobot, mabowx, or WeChat failures on the same host.
---

# Mabobot Hermes

Operate this repository as a local WeChat control plane. Prefer the existing HTTP APIs over direct UI automation or ad hoc code edits.

Assume the project and Hermes run on the same host. Assume the user wants real actions against the live logged-in WeChat account unless they explicitly ask for a dry run.

## Preconditions

- Confirm the repository path is the local `mabobot` project before acting.
- Prefer `http://127.0.0.1:8888` as the main API and `http://127.0.0.1:5555` as the `wx_bot` bridge.
- Treat WeChat actions as production actions. Do not send speculative test messages to real chats unless the user asked for that.
- Prefer API and config changes over editing core code. Edit code only when logs and API behavior show a real defect.

## Core Workflow

1. Confirm service reachability.
   Check main app health, component status, and running processes first.
2. Confirm WeChat reachability.
   Check `/api/wechat/status` and the forced monitor check before sending or diagnosing.
3. Act through the narrowest safe interface.
   Use WeChat APIs for chat operations, plugin APIs for plugin work, and system APIs for logs or recovery.
4. Verify the result.
   Re-check status or logs after every recovery action.

## Common Tasks

### Send or route WeChat messages

- Use `/api/wechat/send-message` for plain replies.
- Use `/api/wechat/add-listen-chat` and `/api/wechat/remove-listen-chat/{chat_name}` to manage listened chats.
- Use `/api/wechat/friends`, `/api/wechat/groups`, and `/api/wechat/my-info` to discover valid chat targets.
- If the task requires file or URL card sending and the main app lacks a matching endpoint, call the `wx_bot` bridge directly.

### Inspect and maintain plugins

- Use `/api/plugins/` to list plugins.
- Use `/api/plugins/stats` to understand active listeners and per-user coverage.
- Use `/api/plugins/{plugin_name}` to inspect one plugin.
- Use `/api/plugins/{plugin_name}/config` to read or update config.
- Use `/api/plugins/{plugin_name}/reload` after config changes when the update endpoint is not used.
- Use `/api/plugins/{plugin_name}/toggle` only when the user clearly wants a plugin enabled or disabled.

### Diagnose failures

- Read `/api/system/logs/app` for backend and plugin failures.
- Read `/api/system/logs/wx_bot` for WeChat bridge or mabowx failures.
- Filter logs with `search=` or `plugin_name=` before assuming a root cause.
- Use `/api/system/components`, `/api/system/status`, and `/api/system/processes` to separate API, plugin, and WeChat issues.

## Recovery Order

Use the least invasive recovery step that can plausibly fix the problem.

1. Re-check health and logs.
2. Re-run `/api/system/wechat-monitor/check`.
3. If WeChat is disconnected or offline, call `/api/system/wechat/restart`.
4. If a plugin is broken, reload only that plugin.
5. If the whole stack is unhealthy and the launcher supports restart signals, call `/api/system/restart/{service}`.
6. Only move to code or config edits after logs point to a concrete defect.

## Guardrails

- Do not edit `.env` or database-backed settings unless the task requires configuration work.
- Do not restart the whole system just because one message send failed once.
- Do not assume the problem is in mabowx if `/api/system/logs/app` points to a plugin exception.
- Do not bypass the API by touching internal modules when an endpoint already exists.
- If a recovery path would require QR scan, manual login confirmation, or Windows desktop interaction, surface that clearly instead of pretending it is fully automated.

## References

- Load [api-and-ops.md](references/api-and-ops.md) for the exact endpoints, payload shapes, and triage checklist.
