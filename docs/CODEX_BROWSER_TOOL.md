# Codex 浏览器工具

Mabobot 通过 Codex App Server 的动态工具接口，为每一轮聊天提供临时的
Playwright Chromium。它用于普通搜索看不到的 JavaScript 页面、动态列表，以及把
当前聊天生成的 HTML 渲染为 PDF 或 PNG；它不是带登录状态的个人浏览器。

## 能力

- `wx_browser.open`：打开公共 HTTP(S) 页面，执行页面 JavaScript，保存渲染后的
  HTML、可见文本和受限数量的 JSON 响应，供当前 Codex 轮次继续分析。
- `wx_browser.fetch_json_pages`：发现分页 API 后，在同一个临时 Chromium 会话中批量
  获取最多 25 个同源 JSON 页面，减少分页期间的数据漂移、重复启动和上下文消耗。
- `wx_browser.render_html`：只读取当前聊天工作区或本次请求目录内的 HTML，在本次
  输出目录生成 PDF/PNG。大型 Microsoft 商城封面目录会自动请求适合目录的缩略图。
- 浏览器临时抓取文件位于本次请求的 `.browser/`，轮次结束后删除；只有 `outputs/`
  中的文件会作为聊天附件收集。

## 权限边界

浏览器由主应用执行，但每次调用都绑定到可信的 `thread_id + turn_id + call_id` 和
当前聊天目录。未绑定、重复、过期或跨轮次调用都会失败关闭。

- 仅允许 `GET`、`HEAD`、`OPTIONS`，仅允许 80/443 端口。
- 拒绝 localhost、局域网、链路本地、保留地址、CGNAT/Tailscale 地址和混合公私
  DNS 结果。
- Chromium 的 HTTP(S) 流量必须经过一次性本机代理；代理把连接固定到已验证的
  公网数字 IP，避免 DNS 重绑定绕过。Codex 本地命令本身仍没有网络权限。
- 使用全新的无痕上下文，不读取 Chrome Profile、Cookie、密码、扩展或本机浏览记录；
  禁用下载和 Service Worker，并阻断 WebSocket。
- 本地 `file://` 子资源只能位于当前聊天工作区或本次请求目录，符号链接和联接点不能
  用来逃逸目录边界。
- 群聊仍共享该群自己的隔离空间，不会因此得到管理员工作区或 Tailnet 管理权限。

这套工具适合公开网页研究，不适合需要登录、提交表单、访问内网后台或进行交易的任务。

## 安装与运维

Python 依赖已在 `requirements.txt` 中声明。首次安装或 Chromium 损坏时，可在
“系统 → 系统工具 → Playwright Chromium”执行修复；命令行等价操作是：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

主要环境变量：

```dotenv
CODEX_BROWSER_TOOL_ENABLED=true
CODEX_BROWSER_MAX_CONCURRENCY=1
CODEX_BROWSER_NAVIGATION_TIMEOUT_MS=90000
CODEX_BROWSER_RENDER_TIMEOUT_MS=120000
CODEX_BROWSER_MAX_REQUESTS=3000
```

修改开关或工具定义后需重启 Mabobot。工具签名变化会轮换旧 Codex 线程，避免旧线程
在没有新权限声明的情况下继续复用。
