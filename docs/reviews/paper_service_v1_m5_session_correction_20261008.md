# M5 分钟数据首日修正记录

2026-10-08 盘前复核发现，正式交易日历包含 `20261008`，但分钟数据采集和准入配置仍把
`20261009` 设为首个合格交易日。这会使项目内 `minute.collect` 调度在 2026-10-08 交易窗口
持续返回 `SKIPPED_BEFORE_ELIGIBLE_SESSION`，错过节后首个交易日。

本次只把以下两项的 `first_eligible_session` 修正为 `20261008`：

- `configs/service/minute_shadow_v1.json`；
- `configs/service/minute_shadow_admission_v1.json`。

采集窗口、证券观察清单、数据源、速率限制和准入阈值均不改变。服务继续保持
`DATA_ONLY`、`research_only=true`、`account_writes_enabled=false` 和
`broker_connected=false`。这项修正只允许项目常驻服务从 2026-10-08 09:31:05 开始归档
分钟数据，不启用分钟模拟成交或任何证券委托。
