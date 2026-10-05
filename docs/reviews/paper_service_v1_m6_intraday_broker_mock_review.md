# Paper Service v1 M6 分钟买入事务 MOCK 评审

## 结论

**`M6_T06_MOCK_SOFTWARE_ACCEPTED`。**

冻结的 `hybrid_intraday_entry_v1` M1/M2 状态机已经接入账户唯一分钟买入事务入口，并通过
MOCK snapshot、重启恢复和故障注入测试。本结论不启用运行服务，不替代真实分钟数据门禁，
也不批准既有模拟账户切换。

## 实现范围

1. `TransactionalIntradayBroker` 注册已审批买单时，在同一账户事务内写入订单、预留、风险
   决策、预留现金和初始执行状态。
2. 状态 v2 将 `ApprovedOrder`、分钟指令、冻结执行配置和涨停价上下文保存在不可变账户事件
   中；重启不依赖内存对象，也不新增重复事实表。
3. 分钟 snapshot 只能通过 `execute_intraday_buy_event` 进入状态机，因此继承
   `INTRADAY_BUYS_ENABLED`、账户状态、snapshot 类型、提交状态和交易日屏障。
4. 每个订单只接受严格单调的 snapshot sequence；使用持久化机器状态，只处理后续输入，
   不用最新修订重算历史成交。
5. M1 与 M2 都使用原有 `IntradayOrderMachine`：
   - M1 使用 09:35 触发后的下一根合格 Bar；
   - M2 额外使用已完成参考 Bar 的 5% 容量并按 100 股向下取整。
6. 成交时原子提交成交、订单终态、预留消费、现金、T+1 持仓、机器状态、processed event、
   watermark、账户领域事件和文字/图表通知 outbox。
7. 取消或窗口过期时原子释放预留；v1 没有分钟卖出、加仓、减仓或部分成交入口。
8. 买入费用默认值与既有 `ExecutionConfig` 一致：双边券商费率 2.5bp、最低佣金 5 元，
   买入过户费 0.1bp；金额以整数微元入账。

## MOCK 验证场景

- 注册订单后，完整状态可以仅从 SQLite 投影和不可变事件恢复；
- M1 在 snapshot 1 进入候选，模拟进程重启后只消费 snapshot 2 并成交；
- 相同成交 snapshot 重放不会产生第二笔成交或第二次资金变化；
- 成交已写、持仓写入前强制故障：成交、现金、订单、预留、执行状态、processed event 和
  outbox 全部回滚；
- 交易窗口过期：订单和预留进入 EXPIRED，预留现金归零；
- M2 容量不足保持 ACTIVE，后续参考 Bar 容量满足后进入候选，再由下一根 Bar 成交；
- 冻结预留低于最大买入成本、账户命名空间不匹配、已有持仓上加仓、snapshot 序号不连续均
  失败关闭。

## 兼容性

状态 v2 只补足重启所需上下文，没有改变入场时点、滑点、容量、涨停或退出政策。恢复器继续
读取旧 shadow 状态和状态 v1；冻结策略回归结果保持不变，黄金基线保存了前后实现哈希。

## 后续状态与尚未通过的门禁

- M6-T07 已在后续的 `paper_service_v1_m6_daily_broker_mock_review.md` 完成 MOCK 软件验收；
  买入成交现已同时形成聚合持仓、逻辑 trade 和 T+1 lot；
- M6-T12—T14 尚未完成；
- M5 真实分钟数据自然门禁尚未通过；
- 未部署账户写入运行时，没有 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED` 结论。

## 验证证据

T06 事务 broker MOCK 测试：

```text
.venv/bin/python -m unittest tests.test_transactional_intraday_broker
Ran 6 tests
OK
```

冻结策略与旧执行语义回归：`Ran 56 tests`，结果 `OK`。

M5 最小冻结运行包与 T06 模块隔离测试通过；T06 使用显式模块导入，不让 data-only 发布包
加载未包含的 `AlphaBu`。

全量回归：`Ran 519 tests`，结果 `OK`。`compileall` 与 `git diff --check` 同时通过。
