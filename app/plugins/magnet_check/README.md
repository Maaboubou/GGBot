# magnet_check

默认只响应消息开头的 `验车 + InfoHash`，由插件补全磁链后查询。例如：

```text
验车 20137867CA87FCBB36E15A730ECB51BD4DE17310
```

`验车` 必须位于消息开头；其前方只允许有空白或配置中的机器人 `@名称`，因此 `帮我验车 ...` 不会触发。40 位十六进制和 32 位 Base32 InfoHash 均受支持。

原来的完整磁链扫描已拆分为 `enable_full_magnet_match` 开关，默认关闭。开启后，消息中有效的 `magnet:?xt=urn:btih:...`（包括带 `tr=http://...` tracker 的长磁链）也会触发检查。普通网页链接不会被消费，仍会继续交给摘要插件。插件调用 whatslink.info 查询资源名称、大小、文件数量和截图，并用 Pillow 直接排版为 PNG 报告图发送到微信。

非磁链消息不会被消费。网络、接口、渲染或发送异常只写入日志，不向微信发送错误提示。

## 权限

在管理后台为目标聊天启用 `magnet_check`，保持 `@Bot Required` 关闭（新权限默认关闭）即可免 `@` 触发。

## 配置

- `timeout`：接口和图片下载超时，默认 30 秒。
- `max_screenshots`：报告最多展示的截图数量，默认 6 张。
- `enable_full_magnet_match`：是否扫描消息中的完整磁链，默认关闭；不影响 `验车 + InfoHash`。
- `detection_threshold`：敏感候选阈值，默认 0.12；越低越严格，也越容易误判。
- `inference_resolution`：NudeNet 输入分辨率，默认 960；本机实测约 0.25 秒/张。
- `use_system_proxy`：是否继承 Windows 系统代理，默认关闭、直接访问资源接口。
- `output_dir`：报告 PNG 保存目录，默认位于插件目录下的 `reports`。
