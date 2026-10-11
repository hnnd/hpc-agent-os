# HPC Agent OS

HPC Agent OS 将资源目录、Slurm 状态、受控操作和产物验收组织为可供智能体及人工 CLI 使用的接口。本仓库是脱敏的软件源码快照：核心包 `hpc-agent-os` 为 0.5.2，应用包 `hpc-agent-os-app` 为 0.6.0。它不包含原项目 Git 历史、生产配置、部署报告、论文或模型试验语料。

主要功能：

- 软件和数据库资源发现，保留版本依据、验证状态、观察时间与访问范围。
- 只读集群查询，共享缓存、限频及显式陈旧/不可用状态。
- 本人作业及预登记维护配方的计划、策略授权、漂移检查、单次领取和对账。
- 应用 CLI 的诊断、资源查询、任务生命周期、只读产物验收和脱敏导出。
- 显式加载的版本化 JSON 插件，以及四个项目 Skill。

## 安装

需要 Linux、Python 3.12；开发环境固定为 3.12.13。使用已有 `uv`，在仓库根目录运行：

```bash
uv sync --locked
uv pip install --python .venv/bin/python --no-deps -e ./application
.venv/bin/hpc-agent-app --mode mock --json doctor
bin/hpc-agent-os --json status
bin/hpc-agent-os --json --plugin "$PWD/application/plugins/example.json" plugins
```

根项目的 `hpc-agent-os` console script 启动核心 MCP 服务；`hpc-agent-app` 和仓库 `bin/hpc-agent-os` 启动应用 CLI，两者用途不同。应用包依赖核心 `hpc-agent-os==0.5.2`；上面的第一步已安装核心及其锁定依赖。应用安装没有被根 `uv.lock` 作为独立包锁定，版本来源为 `application/pyproject.toml`。

默认 mock 不连接真实集群：状态使用合成适配器；未配置模拟目录时资源查询返回不可用，写操作拒绝。该模式不承诺执行真实科学任务。插件用法见 [应用说明](application/README.md)。

## 验证

快速离线检查不启动 PostgreSQL 或 Slurm：

```bash
bash verify.sh --unit
```

完整软件验证需要本项目 `.runtime/postgres/bin` 中的 PostgreSQL 工具。可使用已有 mamba，并显式运行：

```bash
MAMBA_BIN=/absolute/path/to/mamba bash automation/bootstrap-dev.sh
bash verify.sh
PYTHONPATH=src:automation .venv/bin/python automation/demo.py
```

bootstrap 会下载锁定的 Python/Conda 依赖，但不安装系统包、不启动既有服务。PG 集成测试以非 root 用户运行，只创建项目内临时数据目录及单一规范 Unix socket，使用 `hpc_fixture` 和受限角色；拒绝 `PG*` 环境覆盖、TCP、多 host 和符号链接。测试 fixture 在 finally 中停止自己启动的进程。项目路径必须足够短以满足 Unix socket 长度限制；不能用符号链接规避。

`verify.sh` 检查格式、保留的产品测试及 schema；它不是论文冻结输入验收、真实部署验收或模型效果评测。导出契约：

```bash
PYTHONPATH=src:automation .venv/bin/python automation/export_schemas.py
PYTHONPATH=application/src:src .venv/bin/python application/export_schemas.py
```

## 运行边界

真实运行必须另行配置并显式选择 `--mode live`。快照不提供可直接执行的站点部署器或真实策略文件；应用只读桥接配置路径为 `config/live.json`，目录配置为 `config/catalog.json`，操作策略为 `config/phase4-policy.json`。配置结构见 [产品 schemas](registry/schemas)，不得把示例当成生产授权。

策略授权不取消当前主机/UID、资源上限、目录权限、摘要、漂移与一次领取检查。系统不隔离恶意同 UID 程序，不提供多用户身份代理或不可篡改审计；客户端绕过受控入口的 shell 不在本边界内。调度器完成、程序成功、产物满足声明及科学有效性分别判断。用户声明的来源不冒充实测证据。

源码快照范围及修改记录见 [清单说明](SOURCE-MANIFEST.md) 与 [机器清单](SOURCE-MANIFEST.json)。本软件采用 [MIT 许可](LICENSE)。
