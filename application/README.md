# 应用 CLI 与 JSON 插件

应用包 0.6.0 依赖核心 0.5.2。仓库启动器 `bin/hpc-agent-os` 及安装入口 `hpc-agent-app` 均默认 mock；`--mode live` 必须显式选择，并要求配置当前主机及 UID。

```bash
bin/hpc-agent-os --help
bin/hpc-agent-os --json doctor
bin/hpc-agent-os --json status
bin/hpc-agent-os --json software search seqkit
bin/hpc-agent-os --json database search reference
bin/hpc-agent-os --json --plugin "$PWD/application/plugins/example.json" plugins
```

mock 目录未配置时资源查询返回 unavailable；mock 写操作拒绝。`job` 子命令提供 plan、submit、cancel、status、refresh、wait、explain、reconcile；`run` 提供 verify、export，具体参数以 `--help` 为准。真实操作依赖核心策略及 PG 审计，不以本地验收替代任务事实。

插件只支持 API 1.0 的 `owned-script-v1` 模板与结构化诊断规则。插件通过 `--plugin` 显式加载；不自动下载，不导入代码，不接受任意 command/argv。模板只引用用户已准备的 inbox 脚本，摘要冻结后由 Slurm 执行。计划绑定应用代码、插件和产物条件；变更使旧计划失效。

产物验收仅读取声明的任务目录文件，限制路径、数量、大小、所有者及读取漂移。没有规则时不报告验收通过；文件大小/摘要符合要求不证明科学有效。导出排除脚本、原始数据、绝对路径及 argv，并拒绝覆盖已有文件。

插件开发见 [Skill](../.agents/skills/hpc-plugin-developer/SKILL.md)；schema 位于 [schemas](schemas)。执行 `bash application/verify.sh` 等同于根目录完整软件验收；不检查论文冻结数据。
