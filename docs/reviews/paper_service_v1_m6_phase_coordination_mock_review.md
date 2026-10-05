# Paper Service v1 M6 阶段协调与 MOCK 验收评审

## 结论

**`M6_T10_T11_MOCK_SOFTWARE_ACCEPTED`。**

应收/公司行为、开盘卖出和开启分钟买入的阶段事务，以及分钟买入入口的强制阶段屏障，已通过
合成事件和故障注入测试。本结论是软件验收，不是实时分钟数据准入，也没有启用任何账户运行
服务。

## 实现范围

1. `AccountSessionCoordinator` 为以下阶段提供独立入口：
   - 接受单一盘前快照；
   - 应收和公司行为应用；
   - 开盘卖出处理；
   - 开启分钟买入。
2. 每个入口消费一个不可变输入事件，并在同一账户事务内提交领域变化、阶段、阶段版本、
   processed event、stream watermark、账户领域事件和账户版本。
3. 相同输入事件重放直接返回首次结果，不再次执行应收、公司行为或开盘卖出 callback。
4. 阶段由另一个事件完成后，使用不同事件重复声明同一阶段会失败，不产生第二个阶段事实。
5. `execute_intraday_buy_event` 是后续 T06 使用的分钟买入事务入口，并强制校验：
   - 输入事件类型为 `MinuteSnapshotCommitted`；
   - 引用同交易日、`COMMITTED` 的 MINUTE snapshot；
   - 账户阶段精确处于 `INTRADAY_BUYS_ENABLED`；
   - session 没有被阻断；
   - 账户状态为 `SHADOW` 或 `PAPER`，`PAUSED` 不允许买入。

## MOCK 验收边界

MOCK 数据可以且应该用于软件验收，包括：

- 可复现的分钟 Bar 和 snapshot；
- 缺失、乱序、重复、revision 和 stream gap；
- 涨停、停牌、容量不足及候选窗口过期；
- 阶段提前到达、并发版本冲突、事务中断和重启重放；
- D0/M1/M2 的确定性配对比较。

MOCK 数据不能证明真实供应商的延迟分布、覆盖率、字段漂移、真实断流和切源质量。因此本结论
不能替代 M5 的 10 个有效交易日自然数据门禁。

## 已验证故障场景

- 应收 callback 修改现金后抛错：现金、阶段、水位线、processed event 和账户版本全部回滚；
- 相同应收事件重放：callback 只执行一次；
- 盘前阶段未完成时分钟 snapshot 到达：买入 handler 不运行，事件不标记为已处理；
- 完成全部盘前阶段后：同一 MOCK 分钟 snapshot 可进入买入 handler；
- 账户暂停后：即使阶段曾启用分钟买入，新的分钟买入仍被拒绝；
- 输入 snapshot 类型、提交状态或交易日不匹配：失败关闭。

## 尚未通过的门禁

- T06 尚未把 `hybrid_intraday_entry_v1` 的订单状态机和 SQLite 账本接入该唯一入口；
- T07、T12—T14 尚未完成；
- M5 真实分钟数据自然门禁尚未通过；
- 没有 `MINUTE_DATA_ONLY_ACCEPTED` 或 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED` 结论；
- 未部署账户写入运行时。

## 验证证据

事务阶段与 MOCK 分钟屏障专项测试：

```text
.venv/bin/python -m unittest \
  tests.test_account_session_coordinator tests.test_account_session \
  tests.test_preopen_snapshot tests.test_transactional_account \
  tests.test_paper_ledger_store
Ran 39 tests
OK
```

冻结策略与旧执行语义回归：`Ran 55 tests`，结果 `OK`。

全量回归：`Ran 512 tests`，结果 `OK`。`compileall` 与 `git diff --check` 同时通过。
