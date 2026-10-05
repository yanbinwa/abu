# Paper Service v1 M6 盘前阶段屏障软件评审

## 结论

**`M6_T08_T09_OFFLINE_READY`。**

账户交易日阶段状态机和单一盘前快照的软件实现、自测与全量回归已经通过。该结论只允许
继续离线开发 M6-T10—T14，不等于分钟执行或账户运行准入。

本轮没有修改运行中的 M5 data-only 服务，没有创建或推进真实 shadow 账户，也没有让分钟
行情改变任何账户状态。

## 已实现范围

### 账户交易日阶段屏障

- 固定唯一前向路径：
  `CREATED → PREOPEN_INPUTS_READY → RECEIVABLES_APPLIED →`
  `OPEN_SELLS_PROCESSED → INTRADAY_BUYS_ENABLED → DAILY_CLOSE_COMPLETED`；
- 禁止跳阶段、倒退或修改已冻结的盘前快照；
- `phase_version` 提供乐观并发检查，并与账户 `account_version` 在同一事务提交；
- 阶段失败可保存 `blocked_reason`，阻断状态必须显式解除后才能继续；
- 提供稳定的阶段幂等键和 `require_phase` 屏障检查；
- 新交易日 session 必须存在覆盖该交易日的有效 activation。

### 单一盘前快照

- 每个交易日使用稳定流 `market-preopen:{YYYYMMDD}`，只允许一个 sequence 1 快照；
- 快照固定证券主数据、公司行为、证券状态、涨跌停参考、应收截止和待执行订单版本；
- 快照文件、catalog 行和 `PreopenSnapshotCommitted` 领域事件通过既有
  `SnapshotCatalog` 发布；
- 缺失输入仍可形成审计快照，但带有 `PREOPEN_INPUT_MISSING`，不能进入
  `PREOPEN_INPUTS_READY`；
- readiness 同时校验交易日、COMMITTED 状态、文件哈希、快照身份、完整必需输入集合、
  `missing_inputs` 和 `quality_codes`；
- 已提交盘前快照不能被同交易日另一组输入静默替换。

## 关键故障测试

- 跳过盘前阶段直接应用应收：整个账户事务回滚；
- 缺少盘前快照或快照交易日不匹配：拒绝推进；
- 缺少公司行为或证券状态输入：快照可审计，但 readiness 失败；
- 快照文件被修改：哈希校验失败，账户阶段不推进；
- 阶段版本过期：阶段和账户版本均保持不变；
- 阶段更新后强制抛错：阶段、阶段版本和账户版本全部回滚；
- 阻断 session 未显式解除：禁止继续推进；
- 同一完整快照重试：返回原 snapshot/event；同交易日变化后的第二快照不能替换第一份。

## 验证证据

专项测试：

```text
.venv/bin/python -m unittest \
  tests.test_preopen_snapshot tests.test_account_session \
  tests.test_market_snapshot_catalog tests.test_paper_service_contracts
Ran 28 tests
OK
```

全量回归：`Ran 505 tests`，结果 `OK`。`compileall` 与 `git diff --check` 同时通过。

## 尚未通过的门禁

- M6-T10 尚未把应收/公司行为、开盘卖出和开启分钟买入分别绑定到幂等阶段事件；
- M6-T11 尚未把 `INTRADAY_BUYS_ENABLED` 强制检查放进分钟买入事务入口；
- M6-T06—T07、T12—T14 尚未完成；
- M5 自然分钟数据门禁尚未通过；
- 本记录不授权任何正式或既有模拟账户写入。

## 下一动作

实现 M6-T10：由账户输入事件驱动相邻阶段，并确保每个阶段的账本变化、processed event、
领域事件、水位线和账户版本原子提交。随后在 M6-T11 提供唯一的分钟买入事务入口，并在入口
内强制校验 `INTRADAY_BUYS_ENABLED`。
