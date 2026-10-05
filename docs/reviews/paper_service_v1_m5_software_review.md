# Paper Service v1 M5 软件评审

## 结论

**`M5_SOFTWARE_READY_DATA_ONLY`。**

共享分钟行情的软件链路、自测和旧策略回归已经通过；分钟数据仍被严格限制为
`ACCEPT_DATA_ONLY`，不会驱动账户成交。自然时间准入样本目前为 **0/10 个有效交易日**，
因此本评审不等于 `MINUTE_DATA_ONLY_ACCEPTED`，也不解除 M6 的分钟执行门禁。

计划从 **2026-10-09 或之后首个成功归档交易日**开始累计自然样本。

## 已实现范围

1. 动态 watchlist 合并持仓、待执行订单、未终结执行、候选、基准和哨兵证券；watchlist
   使用内容 ID 和单调版本，但分钟流稳定为
   `market-minute:{trading_session}:{interval_minutes}`。
2. `MinuteCollector` 提供有界批次、限流回调、超时 SLA 分类、供应商切换标记和规范化
   字段校验。同步 AKShare 调用由底层适配器负责返回；超过 SLA 后本轮证券失败关闭，v1
   不引入不可安全终止的线程强杀机制。
3. `MinuteBarStore` 保持 append-only，并增加固定 partition manifest、按
   `available_at <= decision_cutoff` 选择 revision、selected event ID 和精确重读能力。
4. 跨证券 snapshot 在所有证券取得终态后一次提交；snapshot、partition、selection、领域
   事件和迟到 revision 审计处于同一 SQLite 事务。
5. 分钟 snapshot 在 watchlist 变化前后保持 sequence 和 predecessor 连续；消费者遇到 gap
   时停止推进。
6. 迟到 Bar 修订只生成审计证据，不重放已经消费的业务 Bar，也不改写交易事实。
7. `IntradayShadowRunner` 增加增量 snapshot 模式，持久化状态机与最后消费序号；冻结的旧
   runner 入口继续保留用于对照。
8. 项目内 `minute.collect` 调度入口已接入 data-only 编排，并以交易日历、首个允许日期和
   盘中采集窗口失败关闭。配置明确禁止 `PAPER` 或账户写入模式。
9. 准入审计输出覆盖率、p95/p99 延迟、revision、gap、数据源切换和未解决错误；正式
   门槛由 `configs/service/minute_shadow_admission_v1.json` 冻结。

分钟基准哨兵使用可由股票分钟接口一致获取的沪深 300 ETF `sh510300`，不把指数代码
`sh000300` 错送给个股分钟接口。

## 关键故障测试

- 50 只证券只完成 24 只：不发布半成品 snapshot；
- 同一 09:35 Bar 后续修订：旧 snapshot 固定旧 revision；
- 两个消费者读取相同 snapshot 和 selected event set；
- 已消费 Bar 出现修订：只记录审计，不再次交给执行状态机；
- W1 切换 W2：分钟 stream 不变，sequence 连续；
- 供应商字段或证券语义不兼容：该证券 `SCHEMA_ERROR`；
- 采集超过 SLA：该证券 `PROVIDER_TIMEOUT`；
- 非交易日、首个允许日期之前、午间窗口：调度任务跳过且不发布快照；
- 配置尝试启用非 data-only 模式：启动失败。

## 验证证据

专项契约和 M5 测试：

```text
.venv/bin/python -m unittest \
  tests.test_paper_service_contracts \
  tests.test_minute_market_hub \
  tests.test_minute_bar_store \
  tests.test_incremental_intraday_shadow \
  tests.test_minute_data_admission \
  tests.test_minute_shadow_job \
  tests.test_market_snapshot_catalog \
  tests.test_event_dispatcher \
  tests.test_service_runtime_freeze
```

冻结策略与旧执行语义回归：

```text
.venv/bin/python -m unittest \
  tests.test_vcp_strategy tests.test_vcp_paper_pipeline \
  tests.test_alpha_forward_shadow tests.test_intraday_execution \
  tests.test_intraday_shadow tests.test_portfolio_executor
```

全量回归：`Ran 469 tests`，结果 `OK`。`compileall` 与 `git diff --check` 同时通过。

## 尚未通过的门禁

- 至少 10 个有效交易日的自然分钟数据尚未产生；
- 每日 snapshot 数、平均覆盖率、p99 延迟和 gap 尚无自然样本；
- 尚未形成 `MINUTE_DATA_ONLY_ACCEPTED` 评审结论；
- M6 不得据此接管任何正式或既有模拟账户写入权。

## 下一动作

发布冻结 runtime，并仅启用 `--enable-minute-shadow` 的 data-only handler；确认运行环境具备
冻结版本的 AKShare 依赖后，从 2026-10-09 起每日审计。自然门禁满足前，可以继续进行不
依赖分钟政策准入的准备工作，但不能让分钟结果改变账户。
