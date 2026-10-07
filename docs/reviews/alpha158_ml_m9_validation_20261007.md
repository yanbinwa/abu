# Alpha158 ML M9 前瞻 Shadow Genesis 验证

- 实现状态：`PASS_GATE_ONLY`
- 真实注册状态：`NOT_ELIGIBLE`
- 已实现：仅 `PASS_HISTORICAL_SCREEN` 可创建不可变 genesis；实验、候选和注册对照必须一致；双账户空仓、等资金；模型/配置/数据产物哈希变化会拒绝运行；重复注册拒绝覆盖。
- 安全边界：研究 shadow 的正式订单通道和交易通知默认关闭，不会自动启动定时任务或切换正式策略。
- 自测：失败候选拒绝、A1/Ridge 错误对照拒绝、产物篡改拒绝、重复注册拒绝均通过。
- 当前结论：M6 的 ElasticNet/LambdaRank 和 M8 的七族受约束组合均未通过预注册历史门槛，因此没有创建真实 shadow genesis，也没有启动自动调度或交易通知。
