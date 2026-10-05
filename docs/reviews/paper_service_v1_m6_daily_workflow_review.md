# M6 事务型账户日编排器软件验收

日期：2026-10-05

## 结论

结论为 `DAILY_WORKFLOW_MOCK_ACCEPTED`。

新增账户日编排器将已经冻结的 `ApprovedOrderCommitted` 事实依次接入订单注册、盘前快照、
应收和公司行为、开盘卖出、开放分钟买入、日终估值与账户对账。编排器不生成选股信号，
不修改风险审批结果，也不改变既有日线退出和分钟买入政策。

## 已验证行为

- 买入订单必须带完整现金和风险预留，否则在进入账户事务前失败关闭；
- 订单必须属于目标账户和交易日；
- 所有订单在 `PREOPEN_INPUTS_READY` 之前冻结；
- 应收、开盘卖出和分钟买入屏障严格按阶段顺序推进；
- 重复运行不会重复注册订单、推进阶段或生成日结；
- 日结只接受已提交 DAILY snapshot 和与事件哈希一致的收盘估值；
- 对账失败不会推进 `DAILY_CLOSE_COMPLETED`；
- 批量作业返回实际消费的 snapshot ID，供作业审计记录使用。

定向组合测试：

```bash
.venv/bin/python -m unittest \
  tests.test_transactional_daily_workflow \
  tests.test_transactional_daily_broker \
  tests.test_transactional_daily_close \
  tests.test_account_session_coordinator \
  tests.test_transactional_intraday_broker
```

结果：29 项通过。

## 剩余接线

当前编排器是可注入固定输入的项目内作业核心。真实运行仍需两个经过版本校验的适配器：

1. 策略和组合风险层将结果发布为不可变 `ApprovedOrderCommitted` 事件；
2. 盘前/收盘行情适配器从已提交 snapshot 生成 `DailyOpenQuote` 和 `DailyClosingMark`。

在这两个适配器完成前，不注册真实账户 handler，也不改变正在运行的数据采集服务。
