# GGBot

GGBot 是运行在 Windows 上的本地微信自动化助手。它通过 Web 管理面板统一管理微信聊天、AI 模型、任务路由、角色、主动回复和插件，不要求把聊天数据或模型密钥交给第三方控制台。

项目自带以下能力：

- 聊天记录：按联系人或群聊保存并检索本地消息。
- AI 助手：支持自定义模型、独立 API Key、任务路由、角色、Judge 主动回复和长期记忆。
- 翻译助手：在聊天中完成文本翻译。
- 汇率查询：查询常用货币汇率。
- 周报助手：调用本机 Codex 整理周报。
- 磁链检查：检查磁力链接内容。
- 菜单翻译：识别菜单图片并生成双语结果。

首次启动不会预置模型、API Key 或聊天绑定。AI 长期记忆默认关闭，由用户自行决定是否启用。内置的“默认助手”和“刘局-和联胜”角色可直接选用，也可以在管理面板中自行创建角色和 Judge。

## 运行要求

- Windows 10/11，且有可交互的桌面会话。
- 微信 4.1 客户端已经安装并登录。
- 64 位 Python 3.11 或 3.12；安装 Python 时勾选 `Add Python to PATH`。
- `wxautox4` 可能需要单独购买并激活授权。
- 菜单翻译需要本机安装 Chrome。Selenium 会为自动化会话创建 Profile，无需手工准备；首次使用可能需要联网获取匹配的驱动。
- 周报助手需要另行安装、登录并配置 Codex CLI；不使用周报时可以暂不安装。

## 一键安装与启动

下载并解压后，直接双击：

```text
一键启动.bat
```

首次运行会自动：

1. 查找 Python 3.11/3.12。
2. 创建项目内的 `.venv`。
3. 安装 `requirements.txt`，包括项目所需的 `wxautox4`。
4. 从 `.env.example` 创建本地 `.env`。
5. 启动微信桥接和 Web 服务，并打开 <http://127.0.0.1:8888/>。

以后再次双击会复用现有环境；只有依赖文件改变时才重新安装。如果只想安装而不启动，可以运行 `Install.bat`。

## 首次配置

### 1. 激活 wxautox4

如果你的授权需要激活，请按照供应商提供的方式执行。常见形式为：

```powershell
.\.venv\Scripts\wxautox4.exe -a <你的激活码>
```

不要把激活码提交到 Git。

### 2. 添加模型并配置任务路由

启动后打开“模型与调用 → 模型连接”，添加自己的模型。添加时只需填写一个 API Key；同一模型供应商可以建立多个模型连接并分别使用不同 Key。凭据保存在本机数据库中，管理页面不会回显原始密钥。

然后在“任务路由”中为需要的任务选择模型，例如：

- `builtin_chatbot.chat`：聊天回复
- `builtin_chatbot.judge`：主动回复判断
- `builtin_chatbot.ocr`：图片理解
- `builtin_translation.translate`：翻译
- `menu_translator.vision`：菜单识别

没有模型时，微信桥接、聊天记录、汇率和磁链检查等非 AI 功能仍可使用。

### 3. 分配聊天权限与角色

在 Web 控制台“聊天”页面添加需要监听的群聊或联系人，并为其启用对应插件。使用 AI 助手时，再为聊天选择角色和 Judge。AI 长期记忆全局默认关闭；需要时到“AI 助手”中手动开启。

## Windows 重启后自动登录

项目提供 `Start-WeChat-AutoLogin.bat`。它会等待 Windows 桌面和微信启动，尝试确认微信登录；确认在线后再启动 GGBot。如果出现二维码，仍需人工扫码。

该脚本不会自行注册为开机任务。可以把它的快捷方式放入 Windows“启动”目录，或在任务计划程序中设置为用户登录后运行。由于微信和窗口自动化依赖交互桌面，不要把任务设置为“无论用户是否登录都运行”。

建议先正常运行一次 `一键启动.bat` 完成环境安装，再双击 `Start-WeChat-AutoLogin.bat` 验证。相关等待时间可在 `.env` 中通过 `WECHAT_AUTOLOGIN_*` 调整；失败日志位于 `logs/wechat_auto_login.log`。如填写 `QQEMAIL_ADDR` 和 `QQEMAIL_CODE`，二维码或登录失败时还可以发送邮件提醒。

## 环境变量

安装器首次运行会把 `.env.example` 复制为 `.env`，以后不会覆盖你的修改。AI 供应商的 API Key 推荐直接在“模型连接”页面填写。

常用字段：

| 字段 | 用途 |
|---|---|
| `WECHAT_BOT_NAME` | 机器人在聊天中的显示名称 |
| `WEB_PORT` | Web 管理面板端口，默认 8888 |
| `WX_BOT_PORT` | 微信桥接端口，默认 5555 |
| `*_API_KEY` | 对应 AI 供应商的本地凭据 |
| `CODEX_PROXY_USE_WSL` | Codex 位于 WSL 时为 `true`，原生 Windows 时为 `false` |
| `QQEMAIL_ADDR` / `QQEMAIL_CODE` | 可选的掉线和自动登录失败通知 |
| `WECHAT_AUTOLOGIN_*` | 自动登录流程的等待和重试参数 |

## 数据、浏览器与安全

管理面板默认只监听 `127.0.0.1`，不要直接暴露到公网。SQLite 数据库默认位于 `data/database.db`；聊天记录、模型配置、角色绑定和任务路由也保存在本机。日志位于 `logs/`，插件生成内容保存在各插件运行目录，这些路径都已被 Git 忽略。

Chrome 自动化 Profile 由 Selenium 在运行时创建和管理。它与日常使用的 Chrome Profile 分离，通常不需要配置或迁移；若清理系统临时文件，下次使用时会自动重新创建。

如果需要重置为全新状态，请先退出 GGBot，备份并移走 `data/`，再重新启动。不要在程序运行时直接删除数据库。

更多说明见 [模型配置](docs/MODEL_CONFIGURATION.md)、[记忆管理](docs/MEMORY_LIBRARY.md) 和 [安全策略](SECURITY.md)。
