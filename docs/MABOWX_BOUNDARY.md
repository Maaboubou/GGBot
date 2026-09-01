# mabowx 与主程序边界

`wx_bot.py` 只负责应用编排，不再针对微信 UI 实现补丁。

## mabowx 负责

- UI 事务锁、窗口激活和目标校验；
- 监听窗口创建、注册、状态快照和 Win32 几何修复；
- 消息源头去重、`delivery_id` 和投递顺序；
- 发送路由选择、精确群聊 `@` 和可证明失败后的单次重试；
- 群资料中“我在本群的昵称”的精确只读解析；
- 折叠文本/链接卡片 URL 提取、内置浏览器关闭和剪贴板恢复；
- 图片、引用图片、视频和文件下载的排序、预览窗口清理、媒体身份校验与剪贴板保护。

## 主程序负责

- HTTP 请求幂等和多段业务回复排序；
- 期望监听列表及其业务状态；
- 数据库、权限、插件事件和审计日志；
- 消息对象的短期缓存，以及下载文件的持久化索引、路径约束和 SHA-256 记录。

## 边界约束

`wx_bot.py` 不得：

- 导入 `mabowx.core.uia` 或遍历微信 UIA 控件；
- 读写 `WeChat.listen` / `listener_manager` 或枚举 `GetAllSubWindow()` 来推断监听状态；
- 替换 `ChatMoreInfoWnd` 等库内类；
- 读写消息的 `_last_*` 私有字段；
- 自行点击、关闭或修复微信窗口。

应用层仅使用 `WeChat.GetListenerStatus()`、`WeChat.SendMsg()`、
`WeChat.GetMyGroupNickname()` 以及消息对象的 `get_url()` / `download()` /
`download_quote_image()` / `get_media_audit()` 等公开入口。
