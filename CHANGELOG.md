# 更新记录

本项目采用语义化版本号。面向使用者的完整公告发布在 GitHub Releases；本文件记录当前公开基线的主要变化。

## [未发布]

### 新增

- `START.bat` 新增首次运行引导：缺少 Python 3.11/3.12（64 位）或 Microsoft WebView2 Runtime 时，经用户确认后从官方源下载安装并验证数字签名，无需新用户预先配置 Python。

### 修复

- 官方登录模式的 Codex Profile 现在可从编辑入口重新选择当前 ChatGPT 账号可用的模型，并同步模型支持的推理强度、上下文窗口和能力信息。

## [3.0.1] - 2026-09-02

### 修复

- Summary Plus 统一通过当前 Python 的 `-m yt_dlp` 调用视频下载器，不再依赖内嵌虚拟环境绝对路径的 `yt-dlp.exe`。
- 修复项目目录改名、移动或备份恢复后，Bilibili 等平台的下载任务立即退出且没有错误输出的问题。
- Web 端 yt-dlp 升级校验改为模块调用；下载失败时同时记录命令、stdout 和 stderr。

## [3.0.0] - 2026-09-02

### 重大变更

- 微信 UI 控制层完整迁移到项目内置的 `mabowx`，不再安装或加载旧自动化库。
- 产品、环境变量、文件工具、运行目录和启动入口统一使用 Mabobot 命名。
- 公开发行版只保留聊天记录、Magnet Check、菜单翻译、Summary Plus、Weekly 和中国银行汇率 6 个插件。

### 新增

- 新增独立桌面启动器，统一管理微信 Bot 与 Web 服务的启动、停止、重启、日志、环境检查和系统托盘。
- 新增 Codex Profile、Skill、聊天隔离、会话连续性、模型路由及文件处理能力；Codex CLI 作为执行框架，Profile 可使用 ChatGPT 账号或第三方 Responses 兼容模型。
- 微信附件建立持久索引，可在后续消息中精确绑定并交给 Codex 处理。

### 修复

- 显式声明 Windows 必需的 `pywin32`，修复全新安装后缺少 `win32api` 的问题。
- 安装器和启动引导会验证 mabowx 实际使用的全部 Win32 模块，并在依赖缺失时自动修复。
- Codex Profile、Skill、运行环境探测和升级命令统一按 UTF-8 读取 WSL 输出，避免 Windows 默认 GBK 导致页面无数据。
- 创建 Codex Profile 前会在实际 Linux/WSL 环境中解析 CLI 的绝对路径，避免新机把 `codex` 命令名误当作不存在的文件路径。

### 安全

- 群聊与普通私聊默认使用按聊天隔离的文件范围；只有显式授权的管理员私聊可以使用本机权限。
- 公开代码及发布增量执行凭据扫描，运行凭据、Cookie、数据库和聊天数据均不纳入版本控制。

[3.0.1]: docs/releases/v3.0.1.md
[3.0.0]: docs/releases/v3.0.0.md
