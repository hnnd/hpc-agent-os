---
name: hpc-plugin-developer
description: 为 HPC Agent OS 开发和修改任务模板、产物验收及结构化诊断 JSON 插件，生成离线测试并验证兼容性。用于插件开发，不用于执行科研作业或安装生物信息软件。
---

# HPC 插件开发

交付可加载的插件 JSON、合成测试用例和实际校验结果。沿用用户指定的工具、插件 ID 和目标功能；有足够信息即可开发，无需为普通项目内编辑另行确认。

## 先确定能力边界

在 HPC Agent OS 项目根目录工作。先读 `AGENTS.md`、`application/README.md` 和 `application/schemas/plugin.schema.json`；需要查实现时遵循项目图谱优先规则。不要因 Skill 被复制到其他位置而假定当前目录就是项目；辅助脚本显式接受 `--root`。

当前 API 1.0 支持两类声明式扩展：

- **任务模板**：`owned-script-v1` 使用用户已准备的本地 inbox 脚本，提供资源默认值和预绑定的产物大小/SHA256 条件。模板本身不生成脚本、安装软件或执行程序；显式请求资源优先于模板默认值。
- **诊断规则**：精确匹配结构化 `state`、`scheduler_state`、`exit_code`、`reconciliation_required`，返回证据、解释和建议。布尔值与字符串不等价；未知或无匹配必须保留 unknown。

如果需求涉及新执行命令、日志正则、文件格式解析、条件组合、自动重试或新的匹配字段，先说明当前 JSON 契约不能表达。用户已经授权框架扩展时，按独立适配器变更实现并测试；否则交付可支持部分并说明所需扩展，不伪造 JSON 字段或声称已实现。不要用 command/argv、Python 导入、eval 或 shell 片段绕过执行器。研究输入与评分变更应使用独立版本，不覆盖既有研究结果。

## 开发

1. 从用户请求提取目标产物、资源约束和诊断证据。只询问影响实现且无法从现有契约推断的缺项；缺少真实软件版本/摘要时不要编造，用合成测试验证插件结构。工具专有语义需要时查该工具官方文档。
2. 插件保存在 `application/plugins/<id>.json`。按当前 schema 和现有示例设计；插件 ID 不使用内置 `core`，成员 ID 在各自类别内唯一，引用使用 `<plugin>:<member>`。修改已使用插件时更新版本，并提醒其摘要变化会使旧应用提案失效。
3. 产物路径相对任务工作目录，不含绝对路径或 `..`。只声明需要的文件，设置合理的字节上限；没有稳定参考结果时不编造 SHA256。大小通过不等于文件格式或科学结论正确。软件/数据库/输入来源声明仍标记 user_declared。
4. 为自定义诊断规则写合成正例、反例和缺失证据场景；尤其测试 `true` 与 `"true"` 的区别，错误码/状态不要用自然语言推断替代。模板测试检查默认资源、显式覆盖与产物边界。具体用例格式及辅助脚本见 [离线验证](references/testing.md)。

## 验证和交付

从项目根目录运行：

```bash
.venv/bin/python .agents/skills/hpc-plugin-developer/scripts/check_plugin.py --root "$PWD" --plugin "$PWD/application/plugins/example.json"
bin/hpc-agent-os --json --plugin "$PWD/application/plugins/example.json" plugins
```

将示例路径换成实际插件；有诊断规则时用 `--cases` 加载合成测试，不能把仅通过结构校验称为行为验证。插件交互/适配器变化补充 `application/tests` 下的 pytest 测试。应用回归命令为 `.venv/bin/pytest -q application/tests`；框架代码或 schema 改动时执行 `application/export_schemas.py`（带 `PYTHONPATH=application/src:src`）和 `bash application/verify.sh`，核对导出契约与实现一致。

开发验收只使用合成材料和 mock；不因为“测试插件”就运行 live、提交 Slurm 作业或访问真实样本。脚本/序列/凭证不进入模型、测试夹具或 Git。Skill 不授予额外生产权限；既有本人策略授权不应被改成额外人工审批。

交付说明写清插件文件、调用方式、实测用例/测试结果、当前不支持的部分。不要把“可加载”“诊断命中”“已执行分析”“科学有效”混为一谈。默认保持插件显式 `--plugin` 加载，不修改客户端配置或自动发布。
