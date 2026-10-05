# Paper Service v1 M6 日频卖出与公司行为 MOCK 评审

## 结论

**`M6_T07_MOCK_SOFTWARE_ACCEPTED`。**

既有“日线退出决策、后续交易日开盘卖出”的时序、T+1、费用、滑点、停牌/跌停延期和公司
行为应收已经映射到事务账户。该结论只覆盖 MOCK 软件验收；没有启用账户写入运行时，也不
替代 M5 真实分钟数据门禁。

## 实现范围

1. 买入成交在原账户事务内建立 `logical_trade` 和带 `sellable_on_session` 的 position lot；
   下一交易日由显式日历输入提供，不用自然日推算。
2. schema v2 新增现金/股份应收和卖出股份预留。服务默认仍打开 schema v1，只有账户 shadow
   在备份后显式请求 v2，当前 data-only 运行时不会被导入代码自动升级。
3. 卖出订单必须指向开放逻辑交易，并按订单有效交易日检查 lot 的 T+1 可卖数量及已有预留；
   同日买入股份不能被卖出预留。
4. 现金分红和送股应收按 `due_session` 应用。阶段事件重放不会重复入账；送股保持原总成本并
   重新计算单位成本。
5. 开盘卖出只接受日频开盘报价。停牌、不可卖、开盘跌停或滑点穿越跌停价时保持 `WAITING`
   和 ACTIVE 股份预留，后续交易日继续处理。
6. 卖出成交、费用、现金、lot、聚合持仓、交易关闭、订单、股份预留、position event、
   processed event、账户事件和文字/图表通知 outbox 在同一 SQLite 事务提交。
7. 没有新增分钟卖出、盘中止损、动态加仓或部分成交政策。

## MOCK 验证场景

- 买入成交生成开放 logical trade 和下一交易日才可卖的 lot；
- 同日卖出预留因 T+1 失败关闭，不留下订单或预留；
- 次交易日卖出成功后关闭 trade、清空 lot/持仓并生成通知 outbox；
- 开盘跌停时订单延期，股份预留保持 ACTIVE，且不生成成交通知；
- 现金及送股应收只应用一次，相同阶段事件重放不二次改变现金或股份；
- 在卖出事实已暂存、订单终态写入前注入故障，成交、现金、lot、trade、阶段、processed
  event 与通知全部回滚；
- v1 数据库不会默认升级，显式 v2 迁移可重复打开且迁移哈希一致。

## 尚未通过的门禁

- M6-T12 日终核算与账户对账、T13 只读投影和 T14 独立配对回放尚未完成；
- 当前运行服务仍是 `ACCEPT_DATA_ONLY`，`account_writes_enabled=false`；
- M5 真实分钟数据自然门禁尚未通过；
- 未批准既有模拟账户切换，也没有 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED` 结论。

## 验证证据

专项集成与故障注入：

```text
.venv/bin/python -m unittest \
  tests.test_operational_schema_v2 \
  tests.test_transactional_intraday_broker \
  tests.test_transactional_daily_broker \
  tests.test_account_session_coordinator
Ran 22 tests
OK
```

全量回归：`Ran 528 tests`，结果 `OK`。`git diff --check` 同时通过。
