# Mabobot API And Ops

Use this reference when the task needs exact endpoints or a tighter recovery loop.

## Main endpoints

- Main app base: `http://127.0.0.1:8888`
- wx_bot base: `http://127.0.0.1:5555`

## WeChat actions

- `GET /api/wechat/status`
- `GET /api/wechat/listened-chats`
- `POST /api/wechat/add-listen-chat`
  Body: `{"chat_name":"目标聊天","exact":false}`
- `POST /api/wechat/remove-listen-chat/{chat_name}`
- `POST /api/wechat/send-message`
  Body: `{"chat_name":"目标聊天","message":"内容","at_users":["昵称1"]}`
- `GET /api/wechat/friends?keywords=...`
- `GET /api/wechat/groups`
- `GET /api/wechat/my-info`

## Plugin actions

- `GET /api/plugins/`
- `GET /api/plugins/stats`
- `GET /api/plugins/{plugin_name}`
- `GET /api/plugins/{plugin_name}/config`
- `PUT /api/plugins/{plugin_name}/config`
  Body: `{"config":{...}}`
- `POST /api/plugins/{plugin_name}/reload`
- `POST /api/plugins/{plugin_name}/toggle`
  Body: `{"enabled":true}`

## System and recovery actions

- `GET /health`
- `GET /api/system/status`
- `GET /api/system/components`
- `GET /api/system/processes`
- `GET /api/system/logs/app?lines=200`
- `GET /api/system/logs/app?lines=200&plugin_name=builtin_chatbot`
- `GET /api/system/logs/wx_bot?lines=200&search=error`
- `GET /api/system/wechat-monitor`
- `POST /api/system/wechat-monitor/check`
- `POST /api/system/wechat/restart`
- `POST /api/system/restart/app`

## Direct wx_bot endpoints

Use these only when the main app does not already expose the needed action.

- `GET /health`
- `POST /api/send_message`
  Body: `{"who":"目标聊天","message":"内容"}`
- `POST /api/add_listener`
  Body: `{"who":"目标聊天"}`
- `POST /api/remove_listener`
  Body: `{"who":"目标聊天"}`
- `GET /api/get_friends`
- `GET /api/get_groups`
- `GET /api/get_my_info`
- `POST /api/send_files`
  Body: `{"who":"目标聊天","file_paths":["C:/path/file.txt"]}`
- `POST /api/send_url_card`
  Body: `{"who":"目标聊天","url":"https://example.com"}`
- `GET /api/is_online`
- `POST /api/restart_wechat`

## Triage checklist

1. Check `GET /health` on the main app.
2. Check `GET /api/system/processes` to see whether `wx_bot.py` exists.
3. Check `GET /api/wechat/status`.
4. Check `POST /api/system/wechat-monitor/check`.
5. Read recent `app` and `wx_bot` logs.
6. If only one plugin is failing, reload only that plugin.
7. If WeChat is offline, restart WeChat first, then verify status again.
8. If the full stack is unhealthy, use the system restart endpoint only when the launcher path is known to support it.

## Local file hints

- App log: `logs/app.log`
- WeChat bridge log: `logs/wx_bot.log`
- Main app startup wires plugin loading, listener sync, and monitor startup in `app/main.py`.
- The WeChat bridge owns low-level send/listen/restart behavior in `wx_bot.py`.
