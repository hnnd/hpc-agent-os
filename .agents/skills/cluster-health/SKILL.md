---
name: cluster-health
description: 查询 Slurm 集群、节点和分区健康摘要，区分实时事实、陈旧快照和未知。用于只读巡检，不用于修复、作业提交或配置变更。
---

# cluster-health

调用 slurm-mcp 的 cluster_summary，必要时调用 list_nodes/list_partitions。所有工具使用 {"request":{"cluster_id":"配置的集群 ID"}}；分页采用 next_cursor，单页 limit≤500。身份来自当前 stdio 进程，不能传 user/role。

先检查 source、observed_at、stale、error，再分析 data。source=synthetic-slurm 时必须明确说明是模拟集群。error 且 data=null 代表未知，不能报告为零作业/空闲；stale=true 时引用旧快照时间并解释刷新失败。分页不完整时不声称覆盖全群。

摘要引用 request_id、source、observed_at；将已验证现象、推断和缺口分开。节点 DOWN 是状态证据，不足以推断硬件故障。工具不可达/权限拒绝时记录阻塞，不调用 SSH、sudo、Shell 或写工具补救。若上游限流，不立即重试，交由缓存周期更新。
