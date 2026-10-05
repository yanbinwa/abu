# Paper Service v1 M6 事务基础软件评审

## 结论

**`M6_T01_T05_OFFLINE_READY`。**

M6-T01—T05 的离线事务基础、自测和冻结策略回归已经通过。该结论只表示账户原子提交、
SQLite 账本映射、输入事件幂等消费以及分钟订单状态增量恢复具备继续集成的基础，不等于
`TRANSACTIONAL_PAPER_SHADOW_ACCEPTED`。

本轮没有把 M6 接入运行中的服务，没有创建或推进真实 shadow 账户，也没有让分钟数据改变
任何账户状态。M5 的 10 个有效交易日自然数据门禁尚未通过，因此 M6-T06 及其后的账户运行
集成仍保持关闭。

## 已实现范围

1. `TransactionalAccountRepository` 为每个账户提供进程内串行命令队列和乐观
   `account_version` 检查；账户业务变更与版本推进处于同一 SQLite 事务。
2. `TransactionalPaperLedger` 将现金、预留现金、持仓、订单、预留、成交、风险决策和订单
   执行投影映射到既有 M0 schema，并对不可变身份冲突、跨账户引用和非法状态转换失败关闭。
3. `execute_event` 以 `(account_id, event_id)` 去重；重复输入直接返回首次提交结果，不重复调用
   handler，也不重复生成成交、账户事件或通知。
4. 单次账户事件事务共同提交：
   - 账户和账本投影；
   - 输入事件处理记录；
   - consumer consumption 与 stream watermark；
   - 不可变账户 domain event 与 `account_events`；
   - 需要通知时的 outbox 和分部状态；
   - 账户版本。
5. `IntradayOrderMachine` 增加完整、带版本的状态导出和恢复；恢复后只消费新的分钟 Bar，
   不重新读取或重算已消费历史。`IntradayShadowRunner` 统一复用这套公开状态契约。
6. 完整状态正文保存在不可变账户 domain event 中，`order_execution_states` 只保存当前投影和
   `last_transition_event_id`。两者不一致或缺少链接时失败关闭，避免新增重复事实源。

## 关键故障测试

- 同一账户四个并发命令：只允许一个版本匹配者提交，其余因旧版本失败；
- 一个账户 handler 失败：该账户的变更和版本完全回滚，不影响另一个账户；
- 相同输入事件并发消费：业务 handler 只执行一次，其余返回首次结果；
- 输入 stream sequence 存在 gap：consumer 注册、水位线和账户变更全部回滚；
- 通知分部非法：账本、领域事件、processed event、outbox 和账户版本全部回滚；
- 不可变订单 ID 以不同事实重用：之前的余额变更一并回滚；
- 终态订单再次成交、跨账户预留：失败关闭；
- 执行投影已写入但账户事件没有携带机器状态：整笔事务回滚；
- 状态机在 09:35 进入候选态后持久化、恢复，只消费 09:37 新 Bar 即得到与不中断运行完全
  相同的成交结果；
- 被篡改的订单事件 sequence：恢复拒绝。

## 验证证据

M6 事务与增量恢复专项测试：

```text
.venv/bin/python -m unittest \
  tests.test_paper_service_contracts \
  tests.test_transactional_account \
  tests.test_paper_ledger_store \
  tests.test_intraday_execution \
  tests.test_intraday_shadow
Ran 41 tests
OK
```

冻结策略与旧执行语义回归：

```text
.venv/bin/python -m unittest \
  tests.test_vcp_strategy tests.test_vcp_paper_pipeline \
  tests.test_alpha_forward_shadow tests.test_intraday_execution \
  tests.test_intraday_shadow tests.test_portfolio_executor
Ran 55 tests
OK
```

本轮只为 `IntradayOrderMachine` 增加状态序列化和恢复能力，没有改变入场时点、价格、容量、
涨停或费用政策；`golden_baselines_v1.json` 保存了原实现哈希、变更范围和新哈希。

全量回归：`Ran 491 tests`，结果 `OK`。`compileall` 与 `git diff --check` 同时通过。

## 尚未通过的门禁

- M5 尚未累计至少 10 个有效交易日的自然分钟样本；
- 尚无 `MINUTE_DATA_ONLY_ACCEPTED` 结论；
- M6-T06—T14 尚未实现或接入运行服务；
- 尚未建立账户交易日阶段屏障、盘前快照、日线卖出/核算集成和独立组合回放；
- 本记录不授权任何正式或既有模拟账户写入。

## 下一动作

保持当前 M5 data-only 服务不变并从 2026-10-09 起累计自然样本。门禁满足后，从
M6-T06 开始把 `hybrid_intraday_entry_v1` 接入独立 shadow 账户；在此之前可以继续评审
本轮事务契约，但不部署账户写入运行时。
