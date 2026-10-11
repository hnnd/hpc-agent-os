# 离线行为验证

先使用实际 Pydantic 模型与 Plugins 注册器校验，不能只靠 JSON Schema：路径规范、总读取预算和重复成员等约束还由运行时验证器执行。辅助脚本不会创建 Backend、数据库连接或 Slurm runner；它只读显式指定的有界 JSON 文件。

`check_plugin.py --root /ABS/PROJECT --plugin /ABS/plugin.json` 只校验结构与注册，返回 behavior_tested=false。脚本使用项目 `.venv/bin/python`；不需要生产配置、模型 API key 或网络。插件与用例路径必须绝对且不经过符号链接。退出码 0=通过，1=断言不匹配，2=插件/用例拒绝。原始异常与文件内容不回显。

诊断场景保存在 `application/plugins/<id>.cases.json`，或在开发时放 `.runtime` 内；文件采用合成观察，不复制实际作业日志。例如针对现有 `application/plugins/example.json`：

```json
[
  {"id":"node-failure","task":{"state":"failed","result":{"scheduler_state":"NODE_FAIL","exit_code":"1:0"}},"expected_rule_ids":["lab:node-failure"]},
  {"id":"normal-completion","task":{"state":"succeeded","result":{"scheduler_state":"COMPLETED","exit_code":"0:0"}},"expected_rule_ids":[]},
  {"id":"missing-evidence","task":{},"expected_rule_ids":[]}
]
```

加 `--cases /ABS/plugin.cases.json` 执行。expected_rule_ids 只列当前插件的完整规则 ID；内置 core 规则不计入该比较。用例数 1..128；空用例集拒绝。允许的观察字段与框架一致：task.state、task.scheduler_state、task.reconciliation_required 和 task.result.scheduler_state/exit_code。没有匹配规则的空预期是正常反例，不能解释为程序运行成功。

每条新增规则应有能命中的证据和至少一个不该命中的场景。若规则匹配布尔值 true，增加字符串 "true" 不命中的用例。status/state 与 scheduler_state 必须按实际契约区分。涉及多规则同时命中时测试完整集合，避免“至少有一条命中”掩盖错误。

任务模板不能仅用诊断场景证明有效。参照 `application/tests/test_application.py` 中的 FakeBackend 和 `application/tests/test_postgres.py`：

- 模板默认资源进入真实 Planner；显式资源覆盖默认值，资源超限拒绝。
- 产物不存在、摘要错误、越界或符号链接时不能通过；未定义产物时验收为未知。
- 修改插件版本/内容后，旧应用绑定执行被拒绝；相同幂等键不能换验收条件。
- 实际任务执行只通过核心受控执行器，以替身 runner 验证，不调用真实 Slurm。

根据插件实际新增的行为选择有区分度的测试，复用框架已有覆盖；不要为每个字段复制同一套测试。新适配器必须证明失败时不执行未经授权的命令，并跑完整应用验收。
