# Paper Service v1 M6 日终核算与对账 MOCK 评审

## 结论

**`M6_T12_MOCK_SOFTWARE_ACCEPTED`。**

日终估值、账户一致性对账和 `DAILY_CLOSE_COMPLETED` 阶段已经接入统一事务。对账失败会保存
独立的 FAILED run 和 CRITICAL finding，但不会消费日结输入、增加账户版本或错误完成账户日。
本结论不启用账户写入运行时，也不是完整 M6 准入结论。

## 实现范围

1. schema v3 新增 `account_daily_closes`，保存现金、可用现金、持仓成本、市值、未实现盈亏、
   权益、持仓数、来源日频 snapshot 和收盘估值哈希。v1、v2 迁移文件保持不变。
2. 收盘估值由 `DailyClosingMarksPrepared` 不可变事件固定，事件同时引用已提交的 DAILY
   snapshot 和 `closing_marks_sha256`；运行时传入不同价格、未来才可见价格或不同 lineage 会
   失败关闭。
3. 日终事务核对：
   - 现金、冻结现金与 ACTIVE 买入预留；
   - 聚合持仓与 position lot 数量、成本和 T+1 可卖数量；
   - OPEN/CLOSED logical trade 与 lot；
   - ACTIVE 订单与现金/股份预留；
   - FILLED 订单与成交事实；
   - 卖出预留与目标交易股份；
   - 到期但未应用的公司行为应收。
4. 对账通过时，daily close、PASSED reconciliation、账户阶段、processed event、stream
   watermark、账户领域事件和账户版本在同一事务提交。
5. 对账失败时，账户事务全部回滚；随后在仍持有账户串行锁时保存 FAILED reconciliation 和
   CRITICAL audit finding，避免并发成功日结被迟到的失败证据覆盖。
6. 修复账本后可以重放相同输入；PASSED run 会解析原 finding 并推进账户阶段。相同成功输入
   再次重放不会创建第二个 daily close。

## MOCK 验证场景

- 100 股持仓按 12 元收盘价估值，持仓成本、市值、未实现盈亏和权益精确入账；
- 相同成功事件重放，账户版本与 daily close 不重复变化；
- 缺少持仓收盘价时保存 FAILED/CRITICAL，阶段与账户版本不推进；
- position 与 lot 数量不一致时拒绝日结且不留下 daily close；
- 修复 lot 后重放同一输入，通过对账并解析 finding；
- 调用方价格与事件固定哈希不一致时直接拒绝，不生成误导性的对账 run；
- v2 升级 v3 时 v2 migration hash 保持不变。

## 尚未通过的门禁

- M6-T13 只读投影导出和 T14 独立组合回放尚未完成；
- 当前服务配置仍为 `account_writes_enabled=false` 和 `ACCEPT_DATA_ONLY`；
- 尚未完成真实分钟数据自然时间门禁，未批准接管既有模拟账户；
- 尚不能给出完整的 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED` 结论。

## 验证证据

```text
.venv/bin/python -m unittest \
  tests.test_operational_schema_v2 \
  tests.test_transactional_daily_close \
  tests.test_transactional_daily_broker \
  tests.test_transactional_account \
  tests.test_account_session_coordinator
```

专项共运行 31 项测试，结果 `OK`；全量回归运行 533 项测试，结果 `OK`；`compileall` 和
`git diff --check` 同时通过。
