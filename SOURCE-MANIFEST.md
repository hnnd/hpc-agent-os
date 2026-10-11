# 软件快照来源清单

`SOURCE-MANIFEST.json` 对每个发布文件记录仓库相对来源路径、原文件 SHA-256 和发布文件 SHA-256。`source_path=null` 表示为本快照新写；抽取函数的文件保留原模块来源及 derivation。清单不含私人绝对路径，不携带原 Git 历史。

白名单范围：核心 0.5.2、应用 0.6.0、数据库迁移、产品 schema/MCP 工具目录、四个项目 Skill、合成示例、受控 fixture 和产品测试。未纳入论文正文、模型评测工具/原始语料、真实资源清单、部署报告、站点配置、个人客户端配置及迁移执行器。

必要变更：

- CatalogConfig 的默认 cluster_id 由站点标识改为 fixture-cluster，并重新导出 schema。
- 应用 live 配置文件名改为 config/live.json；未改变身份、策略、路径、漂移检查。
- 从原 phase4_admin.py/catalog_admin.py 原样抽取 initialize_schema 为 operations_schema.py/catalog_schema.py，去掉部署依赖，更新相关测试导入。
- 产品 schema 导出器去除论文 evaluation 导入/导出；应用导出器说明改为软件快照。
- 新增软件验证脚本、fixture 预检、通用 README/AGENTS/应用说明；Skill 去掉本快照不提供的论文冻结验收命令。
- 未保留历史交付物、站点部署、资源搬迁及论文评测测试。test_catalog_discovery.py 的末尾 FIFO 测试针对未发布的站点 catalog_admin.import_file，因此一并排除；核心目录发现测试保留。
- 根构建配置移除仅针对私有 reports 文件的 sdist 排除项；版本及运行依赖不变；按作者授权为两个软件包声明 MIT 许可并提供许可正文。

这是脱敏软件源码派生快照，不是原冻结研究输入的再发布，也不声称通过原部署、论文或生产完整验收。软件本身保留核心版本号，应用保留 0.6.0；具体派生变更以逐文件摘要为准。

执行 `python automation/check_source_manifest.py` 校验清单中的发布文件。机器清单自身不列入其 files 数组，以避免自摘要循环；Git 提交或外部归档摘要可以进一步绑定机器清单。检查器允许开发时生成的 .runtime/.venv/.cache、字节码与测试缓存，但拒绝其他未列文件。添加 LICENSE、修改 README 或任何发布文件后必须审阅并更新对应发布摘要；新文件以新增记录登记，不能修改来源摘要来掩盖派生变化。
