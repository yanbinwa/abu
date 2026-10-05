# 自闭环多策略模拟交易系统 v1 M0 Review

| 字段 | 结果 |
| --- | --- |
| 日期 | 2026-10-05 |
| 代码基线 | `3030291a5057b36d7b9673e7ddd339df7437e572` |
| Python | 3.11.17 |
| SQLite | 3.53.1 |
| Review 结论 | `APPROVED_FOR_M1` |
| 真实账户影响 | 无 |
| 自动任务影响 | 无；现有 Codex 自动任务保持原状态 |

## 1. 结论

M0 的身份、事务、事件、快照、账户日阶段、作业所有权、通知、容量和切换契约已经
落成可执行 SQL、JSON Schema、冻结配置和契约测试。当前默认配置保持：

```text
research_only = true
broker_connected = false
account_writes_enabled = false
minute_execution_admission = ACCEPT_DATA_ONLY
```

本 Review 只批准进入 M1 运行内核开发，不批准启动常驻服务、写模拟账户、关闭旧自动任务
或迁移现有状态。

## 2. 交付物

### 2.1 设计与计划

- `docs/specs/self_contained_paper_trading_service_v1.md`
  - SHA256 `488be57bc06f8f0304090b21e0541577d0b6b3d885634002b03a4f6bacad7ade`
- `docs/plans/self_contained_paper_trading_service_v1_development_plan.md`
  - SHA256 `8fb2d3f96292a45a5c9435a9cd6fdf93dd9c629d5873d2e201a7e036ba76b74b`

### 2.2 数据库和契约

- `abupy/ServiceBu/schemas/operational_v1.sql`
  - SHA256 `47e427c0dfc116a45cd2ffd55fdc7e73d9da9af013dfc5b3ee0ba1f3274e68b7`
- `abupy/ServiceBu/schemas/README.md`
- JSON Schema：
  - domain event；
  - daily snapshot；
  - minute snapshot；
  - watchlist；
  - preopen snapshot；
  - strategy activation；
  - notification；
  - backup manifest。

### 2.3 冻结配置

- `configs/service/service_v1.json`；
- `configs/service/jobs_v1.json`；
- `configs/service/job_ownership_v1.json`；
- `configs/service/accounts_v1.json`；
- `configs/service/retention_v1.json`；
- `configs/service/numeric_policy_v1.json`；
- `configs/service/reason_codes_v1.json`；
- `configs/service/golden_baselines_v1.json`。

### 2.4 测试

- `tests/test_paper_service_contracts.py`
  - SHA256 `351d3a96c4551d166d80d140bac0548714c2600fbe60ac745c91ced256667e80`

## 3. 已冻结决策

1. 单机 v1 使用 OS 排他文件锁，不使用可过期租约。
2. SQLite 是账户、作业、事件消费和通知 outbox 的权威运行状态。
3. 快照 `COMMITTED` 和对应 domain event 在同一事务提交。
4. 分钟 stream 固定为 `market-minute:<session>:<interval>`；watchlist 变化不换流。
5. 分钟快照固定 cutoff、revision、selected event set、序号和前驱。
6. 账户只增量推进订单状态，迟到修订不改写已发生成交。
7. 账户交易日必须依次经过盘前输入、应收、开盘卖出和分钟买入屏障。
8. 账户 active activation 管理新决策；逻辑交易独立冻结 management activation。
9. 现有自动任务按逐作业所有权迁移；组合旧任务拆分前不可部分切换。
10. 首次新系统账户事件提交后禁止恢复旧状态快照。
11. 企业微信采用持久化重试，允许重复，不承诺外部必然送达。
12. v1 不自动删除 domain events 或其他权威事实，只自动清理可重建产物。
13. 金额和价格在权威账户库中使用整数微元，数量使用整数股。

## 4. 现有自动任务盘点

已登记且仍由旧 owner 运行：

| 旧 owner | 作业 | 是否写账户 |
| --- | --- | --- |
| `codex-automation:vcp` | VCP 日频、短线收盘、账户和通知组合任务 | 是 |
| `codex-automation:alpha158` | Alpha158 日频、账户和通知组合任务 | 是 |
| `codex-automation:v1-3` | 分钟研究账户日线订单准备 | 是 |
| `codex-automation:v1` | 全天分钟数据和 shadow | 否 |
| `codex-automation:v1-2` | 分钟日终审计和准入 | 否 |
| `codex-automation:shadow` | 短线竞价代理归档 | 否 |

所有条目的 `cutover_session` 仍为空，状态为 `OLD_OWNER_ACTIVE`。M0 没有修改、暂停或
删除任何自动任务。

## 5. 自测证据

### 5.1 M0 契约测试

```text
.venv/bin/python -m unittest tests.test_paper_service_contracts
Ran 9 tests
OK
```

覆盖：

- 全部 JSON Schema 示例；
- SQL schema 重复创建；
- job run 与 attempt 分离；
- stream sequence 唯一约束；
- 稳定分钟 stream 不包含 watchlist ID；
- 账户交易日阶段枚举；
- 安全默认配置；
- 当前旧自动任务所有权覆盖；
- 黄金文件哈希一致性。

### 5.2 策略与执行相邻回归

```text
.venv/bin/python -m unittest \
  tests.test_vcp_strategy \
  tests.test_vcp_paper_pipeline \
  tests.test_alpha_forward_shadow \
  tests.test_intraday_execution \
  tests.test_intraday_shadow \
  tests.test_portfolio_executor
Ran 53 tests
OK
```

### 5.3 完整回归

```text
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 401 tests
OK
```

M0 前完整基线为 392 项；新增 9 项契约测试后总数为 401，原有测试全部通过。

## 6. M1 约束

M1 可以实现：

- OS 文件锁；
- SQLite 初始化和迁移；
- job runs/attempts；
- 调度器外壳；
- 心跳；
- SQLite Online Backup 和恢复 CLI；
- launchd 示例。

M1 仍然禁止：

- 自动采集真实市场数据；
- 写入现有或新模拟账户；
- 启用分钟执行；
- 发送企业微信；
- 停止或替换任何现有自动任务。

## 7. 开放项

以下内容按计划进入后续阶段，不阻塞 M1：

- APScheduler 的具体冻结版本在 M1 引入依赖时记录；
- 数据供应商的真实延迟和覆盖在 M3/M5 自然时间观察；
- 现有组合自动任务的拆分在对应作业切换前完成；
- 正式账户导入和 cutover marker 在 M9 才允许实现和启用。
