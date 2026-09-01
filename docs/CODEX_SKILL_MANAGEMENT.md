# Codex Profile Skill 管理

控制台为每个 Codex Profile 提供独立的 Skill 管理入口。打开“Codex 运行中心”，在目标 Profile 卡片上选择 **Skills**。

## 当前能力

- 查看 Profile 自定义 Skill 与 Codex 内置系统 Skill。
- 创建 instruction-only Skill，自动生成符合 Agent Skills 规范的 `SKILL.md` frontmatter。
- 从公开 GitHub 仓库安装包含脚本、参考资料和模板的完整 Skill 目录。
- 查看及编辑自定义 `SKILL.md`；系统 Skill 只读。
- 按 Profile 启用或停用 Skill。停用项写入该 Profile 的 `config.toml`，并定向重启该 Profile 的 Codex 运行时。
- 编辑前自动备份旧版本；删除操作实际移入 Profile 回收站，可以在页面恢复。
- 记录创建、编辑、启停、归档和恢复审计事件，不记录 Skill 正文。

Profile 间不会共享页面创建的 Skill。文件位于对应的受管目录：

```text
$CODEX_HOME/skills/<skill-name>/SKILL.md
```

管理元数据、历史和回收站位于同一 Profile 的 `CODEX_HOME` 下，并使用 `.mabobot-skill-*` 前缀；这些目录不在 Codex 的 Skill 扫描根目录内。

管理 API 只接受受管 Profile 根目录内的普通目录和文件，不跟随符号链接，也不把 Skill 正文拼进 shell 命令。聊天中的 Codex 运行时只有这些 Skill 资产的读取权限；写入只能由控制台的管理接口完成。

## 从 GitHub 安装

在目标 Profile 的 Skills 窗口选择 **从 GitHub 安装**，填写：

- 公开仓库地址，例如 `https://github.com/hugohe3/ppt-master`。
- 仓库内 Skill 路径，例如 `skills/ppt-master`。最后一段必须与 `SKILL.md` 的 `name` 一致。
- 分支、标签或提交；生产使用建议填写审核过的提交哈希，而不是长期跟随 `main`。

安装器使用 WSL 中的 Git sparse checkout，只获取指定 Skill 子目录；Git 凭据和交互式认证被隔离，因此页面入口不能读取本机 SSH Key 或安装私有仓库。客户端不能指定本机目标目录。下载内容先进入 Profile 下不可被 Codex 发现的随机暂存目录；通过以下校验后才原子移入 `skills/`：

- `SKILL.md` frontmatter、名称和 256 KiB 上限。
- 禁止符号链接和特殊文件。
- 最多 25000 个文件、解压后最多 512 MiB、单文件最多 64 MiB。
- 记录来源仓库、ref、内容 SHA-256、文件数量、总大小和依赖清单。

安装过程不会执行第三方 Skill 脚本，也不会自动运行 `pip`、`npm` 或安装 `requirements.txt`、`pyproject.toml`、`package.json` 中的依赖。页面会显示检测到的依赖声明，依赖安装需要管理员另行审核和处理。当前仅支持公开仓库；不通过页面传递 GitHub Token。

## 命名与内容规则

- 名称为 1–64 个字符，只允许小写字母、数字和单个连字符；不能以连字符开头或结尾。
- `description` 必须说明 Skill 能做什么以及何时触发，最长 1024 个字符。
- `SKILL.md` 最大 256 KiB。编辑时 frontmatter 的 `name` 必须与目录名一致。

Codex 会先读取所有 Skill 的 `name` 与 `description`，命中任务后再加载完整指令，因此触发描述应简洁且明确。

## 当前边界

页面新建流程聚焦 instruction-only Skill。GitHub 安装可以导入已有的 `scripts/`、`references/`、`assets/` 和 `agents/openai.yaml`；页面会标记“含支持文件”，编辑 `SKILL.md`、归档和恢复都不会删除这些文件，但暂不提供逐文件编辑。

GitHub Skill 当前按 Profile 独立安装，不会自动共享给其他 Profile。需要连同 MCP、连接器或账号授权分发时，应使用 Plugin 流程。

格式与加载行为参考 [OpenAI Build skills 文档](https://learn.chatgpt.com/docs/build-skills) 和 [Agent Skills 规范](https://agentskills.io/specification)。
