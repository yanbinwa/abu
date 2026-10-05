# 自闭环多策略模拟交易系统 v1 开发计划

| 字段 | 内容 |
| --- | --- |
| 状态 | In progress；M3 data-only shadow 等待 5 日门禁，M4-T01—T09 软件完成、T10 暂缓 |
| 计划版本 | 0.1.0 |
| 创建日期 | 2026-10-05 |
| 对应规格 | [自闭环多策略模拟交易系统设计 v1](../specs/self_contained_paper_trading_service_v1.md) |
| 候选代码基线 | `3030291`；实施开始时重新记录实际 `HEAD`、分支和工作区状态 |
| 当前阶段 | M3 自然时间观察与 M4 隔离软件开发并行；未批准 M4 自然时间账户运行 |
| 运行边界 | 研究、影子盘和模拟盘；不连接券商，不发送真实委托 |

## 1. 计划目标

本计划将设计规格拆分为可以逐项实现、自测、故障注入和 Review 的工程阶段，最终形成：

1. 不依赖 Codex 自动任务的项目内常驻调度服务；
2. 统一、增量、版本化的日频和分钟行情数据中心；
3. 多策略共享数据、独立账户和统一模拟撮合；
4. 账户状态、事件消费和通知 outbox 的事务一致性；
5. 可恢复、可重放、可对账、可解释的模拟交易链路；
6. 只读可视化和带交易K线图的企业微信通知；
7. 在不改变现有策略语义的前提下迁移 VCP 和 Alpha158；
8. 继承现有分钟执行的 10 日校准、冻结后 20 日观察和正式回放门禁。

本计划不以收益提高作为软件完成标准。完成标准是状态正确、时间因果正确、故障可恢复、
多账户隔离、结果可审计，并且现有策略在同一输入下可以复现。

## 2. 开发原则

### 2.1 契约先于运行代码

- M0 未通过 Review 前，不新增正式账户写入服务；
- 数据库 schema、事件 envelope、快照 schema 和状态机先通过示例和测试；
- 实现中发现契约不足时先更新规格并 Review，不在代码里形成隐式规则。

### 2.2 现有策略语义冻结

- v1 保持“日线选股、分钟买入执行、日线退出与核算”；
- 不在本计划中实现分钟止损、盘中卖出、动态加仓或减仓；
- 不调整 VCP、Alpha158 的信号、排名、止损或风险参数；
- 策略差异必须由黄金结果测试识别并解释。

### 2.3 单一权威状态

- SQLite 是账户、作业、领域事件、消费进度和通知 outbox 的权威运行状态；
- 不可变文件是行情原始内容、规范化分区和快照 manifest 的权威数据；
- JSON、CSV、HTML、PNG 和日报都可以删除后重建；
- 不允许同一事实同时由文件和数据库独立写入并互相覆盖。

### 2.4 先 shadow，后接管

- 新数据中心先与现有脚本并行对比；
- 新策略适配器先写独立 shadow 账户；
- 新分钟服务先只归档数据，不影响任何账户；
- 所有准入证据通过后，才在交易日边界切换唯一写入者。

### 2.5 故障注入是完成条件

- 每个有事务或外部副作用的阶段都必须包含进程退出测试；
- “正常路径测试通过”不足以完成里程碑；
- 恢复后的账户、事件水位线和通知状态必须可证明，而不是人工观察。

### 2.6 保护现有工作区和运行状态

- 实施前记录 `git status --short`，不覆盖或顺带提交无关修改；
- 优先在用户指定的独立 worktree 实施；未明确要求前不自行创建 worktree；
- 当前 VCP、Alpha158 和分钟 shadow 状态不原地迁移；
- 每次迁移使用新目录、显式 schema 版本和可回滚切换记录。

## 3. 范围

### 3.1 包含

- 单机项目内调度器和 OS 文件锁；
- SQLite schema、迁移、事务和在线备份；
- 作业运行与 attempt 历史；
- 事务型领域事件 outbox、消费进度和流水位线；
- 日频与分钟跨证券快照；
- revision 选择、迟到事件和数据修订审计；
- 策略注册、activation、稳定账户身份和持仓管理归属；
- VCP、Alpha158 适配器及黄金结果测试；
- 共享分钟 watchlist、采集、存储和数据 shadow；
- `hybrid_intraday_entry_v1` 模拟执行接入；
- 企业微信文字、图片分片和持久化重试；
- 系统健康、数据覆盖和多账户只读可视化；
- 日终对账、备份恢复和正式模拟账户切换流程。

### 3.2 不包含

- 真实券商网关；
- 多机或多主高可用；
- tick、盘口队列和部分成交；
- 分钟选股和盘中横截面排序；
- 盘中止损、卖出、加仓和减仓；
- 自动调参和策略自动晋级；
- 采购长期分钟历史数据；
- 使用不满足 PIT 的财报数据进行历史回填。

## 4. 目标代码和配置结构

文件名在 M0 Review 中可以小幅调整，但职责边界不得合并回单体脚本。

```text
abupy/ServiceBu/
  __init__.py
  ABuServiceRuntime.py
  ABuServiceLock.py
  ABuScheduler.py
  ABuJobStore.py
  ABuOperationalStore.py
  ABuSchemaMigration.py
  ABuDomainEventStore.py
  ABuEventDispatcher.py
  ABuMarketSnapshotCatalog.py
  ABuDailyMarketHub.py
  ABuMinuteMarketHub.py
  ABuStrategyRegistry.py
  ABuPaperBroker.py
  ABuNotificationOutbox.py
  ABuReconciliation.py
  ABuDashboardQuery.py

abupy/ServiceBu/schemas/
  domain_event_v1.json
  daily_snapshot_v1.json
  minute_snapshot_v1.json
  watchlist_v1.json
  strategy_activation_v1.json
  notification_v1.json

configs/service/
  service_v1.json
  jobs_v1.json
  job_ownership_v1.json
  providers_v1.json
  accounts_v1.json
  retention_v1.json
  notification_v1.json
  strategies/
    vcp_v1.json
    alpha158_v1.json

scripts/
  run_abu_service.py
  audit_abu_service.py
  backup_abu_service.py
  restore_abu_service.py
  reconcile_abu_accounts.py
  render_abu_dashboard.py

deploy/launchd/
  com.abupy.paper-service.plist.example

docs/runbooks/
  paper_service_operations_v1.md
  paper_service_recovery_v1.md
  paper_service_cutover_v1.md
```

预计修改的现有模块：

- `abupy/AlphaBu/ABuTradeIntent.py`：账户和策略实例命名空间；
- `abupy/AlphaBu/ABuPortfolioExecutor.py`：领域状态导入/导出和事务适配接口；
- `abupy/AlphaBu/ABuPositionLedger.py` 或等价持仓账：activation 和 lot 管理归属；
- `abupy/MarketBu/ABuMinuteBarStore.py`：按固定 manifest 和 selected event ID 读取；
- `abupy/AlphaBu/ABuIntradayExecution.py`：增量状态转换接口，不从全部历史重算；
- `abupy/AlphaBu/ABuVCPPaperTrading.py`：只增加适配器/导入导出，不直接改现有状态；
- `abupy/AlphaBu/ABuAlphaForwardShadow.py`：只增加适配器/导入导出；
- `services/wecom_bot/bot.mjs`：消费数据库派生投递任务或受控桥接文件。

## 5. 里程碑总览

| 里程碑 | 主题 | 主要结果 | 依赖 | 账户影响 |
| --- | --- | --- | --- | --- |
| M0 | 契约、schema 与黄金基线 | 可冻结的设计和测试夹具 | 无 | 无 |
| M1 | 运行内核和操作数据库 | 单实例、作业、迁移、备份 | M0 | 无 |
| M2 | 领域事件与快照目录 | 不丢事件、可恢复消费 | M1 | 无 |
| M3 | 日频数据和因子快照 | 统一日频数据版本 | M2 | shadow |
| M4 | 策略注册和多账户账本 | VCP/Alpha158 黄金结果一致 | M2、M3 | 独立 shadow 账户 |
| M5 | 共享分钟行情中心 | 跨证券快照、revision 和水位线 | M2 | data-only |
| M6 | 事务模拟券商和分钟买入 | 账户原子提交、增量执行 | M4、M5 | 独立 shadow 账户 |
| M7 | 通知和K线图 | 持久化重试、文字图片分离 | M6 | 不改变交易 |
| M8 | 可视化、对账和运维 | 只读控制台、备份恢复 | M3—M7 | shadow |
| M9 | 自然时间准入和切换 | 唯一写入者切换 | M5—M8 | 审批后接管 |

关键路径：

```text
M0 -> M1 -> M2 -> M3 -> M4 -> M6 -> M7 -> M8 -> M9
                  \         /
                   -> M5 ---
```

M5 的软件开发可以与 M4 后半段并行，但 M6 必须等待 M4 和 M5 均通过。M9 包含自然时间
门禁，不能通过增加开发并行度缩短。

实施状态：

| 里程碑 | 状态 | Review |
| --- | --- | --- |
| M0 | 完成 | `docs/reviews/paper_service_v1_m0_review.md` |
| M1 | 完成 | `docs/reviews/paper_service_v1_m1_review.md` |
| M2 | 完成 | `docs/reviews/paper_service_v1_m2_review.md` |
| M3 | 软件完成，等待自然时间门禁 | `docs/reviews/paper_service_v1_m3_review.md` |
| M4 | T01—T09 软件完成；T10 等待 M3 准入 | `docs/reviews/paper_service_v1_m4_software_review.md` |
| M5 | 软件完成，等待自然时间门禁 | `docs/reviews/paper_service_v1_m5_software_review.md` |
| M6 | T01—T06、T08—T11 离线软件完成；未接入账户运行服务 | `docs/reviews/paper_service_v1_m6_intraday_broker_mock_review.md` |
| M7—M9 | 未开始 | — |

## 6. M0：契约、schema 与黄金基线

### 6.1 目标

在修改运行逻辑前，冻结所有会影响一致性、恢复和策略行为的契约。

### 6.2 任务

- [ ] M0-T01 记录实际代码基线、Python/SQLite/AKShare 版本和完整工作区状态。
- [ ] M0-T02 盘点当前 VCP、Alpha158、分钟 shadow、企业微信和 Codex 自动任务的
  输入、状态目录、写入者、执行时间和恢复方式。
- [ ] M0-T03 定义 SQLite schema v1，覆盖规格第 18 节所有表、外键和唯一约束。
- [ ] M0-T04 定义 schema migration 规则：版本表、升级事务、失败回滚和禁止降级。
- [ ] M0-T05 定义 `domain_event_v1` envelope、stream、sequence、前驱和内容哈希。
- [ ] M0-T06 定义 `event_consumers`、`event_consumptions`、stream watermark、gap 等待、
  迟到事件和人工归档状态机。
- [ ] M0-T07 定义日频和分钟快照 JSON Schema；分钟 schema 固定 selected revision、
  `selected_bar_set_sha256`、前驱和单调序号。
- [ ] M0-T08 定义账户、策略实例、activation 和配置切换 schema。
- [ ] M0-T09 定义 `logical_trade`、position lot、management activation 和 takeover 事件。
- [ ] M0-T10 定义账户事务命令、写入顺序、领域事件和通知 outbox 提交边界。
- [ ] M0-T11 定义 `job_runs` 与 `job_run_attempts` schema 和状态机。
- [ ] M0-T12 定义通知分片、`UNKNOWN` 重试、人工处置和审计 schema。
- [ ] M0-T13 定义 OS 文件锁路径、权限、服务实例和异常退出语义。
- [ ] M0-T14 定义文件原子发布、父目录 fsync 和 backup manifest 格式。
- [ ] M0-T15 保存当前 VCP 和 Alpha158 的确定性黄金夹具及运行命令。
- [ ] M0-T16 保存当前分钟状态机边界夹具，覆盖 09:35、午休、截止时间和修订。
- [ ] M0-T17 建立统一原因码登记表，禁止自由文本作为状态判断依据。
- [ ] M0-T18 创建 `docs/reviews/paper_service_v1_m0_review.md` 模板和证据索引。
- [ ] M0-T19 建立现有全部自动任务的逐作业所有权矩阵，区分数据、因子、shadow、
  正式账户、通知和报告。
- [ ] M0-T20 冻结账户交易日阶段状态机、盘前快照 schema 和各阶段幂等键。
- [ ] M0-T21 冻结三类数据保留、磁盘 warning/critical 水位和禁止自动删除权威数据规则。
- [ ] M0-T22 冻结 M3、M8 的有效交易日、成功率、截止时间和观察重置标准。
- [ ] M0-T23 汇总全部证据并完成 M0 Review，记录批准项和开放问题。

### 6.3 自测

- JSON Schema 接受全部有效示例，拒绝缺失版本、序号、哈希和账户身份的示例；
- SQL schema 可在空数据库一次建成，重复执行无副作用；
- 所有唯一键均有碰撞测试；
- 相同输入两次生成相同事件 ID、快照 ID 和内容哈希；
- 不同账户的相同策略和订单不会发生 ID 冲突；
- 旧持仓在配置升级前后均可解析到唯一管理政策；
- watchlist 变化前后仍属于同一个交易日分钟 stream；
- 分钟买入在账户未进入 `INTRADAY_BUYS_ENABLED` 时必然被拒绝；
- 每个旧、新作业都有唯一 owner 和明确切换边界；
- 黄金夹具连续运行两次，核心产物哈希一致。

### 6.4 退出条件

- 规格状态可以从“M0 输入草案”升级为“设计冻结”；
- schema、示例、状态机和原因码均有版本；
- 事务边界、快照选择、迟到事件和通知未知状态没有开放歧义；
- 动态 watchlist 使用稳定交易日分钟 stream；
- 账户交易日阶段屏障和盘前快照契约已经冻结；
- 逐作业所有权、首次新提交前后回滚边界和旧入口拒写方式已经冻结；
- 消费者生命周期、数据保留、磁盘水位和 M3/M8 定量退出门禁已经冻结；
- M0 Review 结论为 `APPROVED_FOR_M1`。

## 7. M1：运行内核和操作数据库

### 7.1 目标

建立不执行交易的常驻服务骨架、单实例保护、作业运行历史和可靠备份。

### 7.2 任务

- [ ] M1-T01 实现 `ABuServiceLock`，全生命周期持有 OS 排他文件锁。
- [ ] M1-T02 实现 `service_instances`，记录进程、主机、启动和停止原因。
- [ ] M1-T03 实现 SQLite 初始化、WAL、外键、busy timeout 和 schema migration。
- [ ] M1-T04 实现 `ABuJobStore`，分离 `job_runs` 和 `job_run_attempts`。
- [ ] M1-T05 实现作业幂等键、重试、超时恢复和结构化错误。
- [ ] M1-T06 引入并冻结项目内调度依赖，加载 `jobs_v1.json`。
- [ ] M1-T07 实现交易日历门禁和 `Asia/Shanghai` 时钟检查。
- [ ] M1-T08 实现健康心跳、最近成功作业和服务状态输出。
- [ ] M1-T09 实现 SQLite Online Backup API 备份与 backup manifest。
- [ ] M1-T10 实现空数据库恢复、备份恢复和哈希校验 CLI。
- [ ] M1-T11 提供 launchd 示例；其中不包含策略业务规则和密钥。

### 7.3 自测与故障注入

- 两个服务实例同时启动，只有一个取得文件锁；
- 持锁进程暂停时第二实例仍不能启动；
- 进程异常退出后锁自动释放，新实例能够启动；
- 同一作业幂等键只产生一个 `job_runs`，多次失败保留全部 attempts；
- 成功作业重启后不重复执行；
- SQLite 主文件存在未 checkpoint WAL 时，备份仍包含已提交事务；
- 从 backup manifest 恢复空目录后，数据库完整性检查通过；
- 系统时区或时钟不满足要求时交易类作业不会启动。

### 7.4 退出条件

- 服务可由命令行和 launchd 启动，但尚未执行市场采集和账户交易；
- 连续重启和故障注入不丢作业历史；
- M1 Review 结论为 `RUNTIME_KERNEL_ACCEPTED`。

## 8. M2：领域事件和快照目录

### 8.1 目标

建立快照提交、领域事件创建、分发、消费和恢复的一致性基础。

### 8.2 任务

- [ ] M2-T01 实现 `domain_events`、`event_consumptions` 和 `stream_watermarks`。
- [ ] M2-T02 实现同事务 `COMMITTED snapshot + domain event` 提交接口。
- [ ] M2-T03 实现重复投递和消费者幂等确认。
- [ ] M2-T04 实现消费者登记、生效序号、必需标记和退休边界。
- [ ] M2-T05 实现 stream sequence、previous event 和 gap 检测。
- [ ] M2-T06 实现 `WAITING_FOR_GAP`、超时告警和按流类型失败关闭。
- [ ] M2-T07 实现迟到事件分类和显式更正/审计事件。
- [ ] M2-T08 实现内容寻址文件写入、同文件系统临时文件和父目录 fsync。
- [ ] M2-T09 实现 manifest 校验、SQLite 发布和孤儿文件审计。
- [ ] M2-T10 实现启动时未消费事件扫描和恢复分发。
- [ ] M2-T11 实现只读快照目录查询接口。
- [ ] M2-T12 实现审计式人工归档检查；v1 不自动删除 domain events。

### 8.3 自测与故障注入

- manifest 已落盘、SQLite 事务前退出：只有安全孤儿，无已发布快照；
- 快照和事件事务提交后、首次分发前退出：重启后事件仍被消费；
- 分发后、消费确认前退出：重复投递但消费者只生效一次；
- 事件序号缺失：消费者等待且不推进水位线；
- 迟到分钟修订：只产生审计，不改变既有业务事实；
- 相同事件 ID 不同 payload：停止对应流并生成严重告警；
- 新消费者从登记的生效序号开始，不追溯阻塞创建前历史；
- 必需消费者未确认或未完成退休边界时事件不能归档；
- 数据库引用的 manifest 缺失或哈希错误：快照转 `CORRUPT`，下游失败关闭。

### 8.4 退出条件

- 不存在快照已提交但永久丢失就绪事实的窗口；
- 每个消费者都可从持久化水位线恢复；
- M2 Review 结论为 `EVENT_AND_SNAPSHOT_CORE_ACCEPTED`。

## 9. M3：统一日频数据和因子快照

### 9.1 目标

把现有日线、指数、行业、估值、公司行为和 PIT 数据采集组织成统一快照，不改变策略。

### 9.2 任务

- [ ] M3-T01 盘点并适配现有日频下载和模拟盘行情更新脚本。
- [ ] M3-T02 实现交易日级增量计划和供应商限流。
- [ ] M3-T03 统一原始响应归档、规范化数据和来源元数据。
- [ ] M3-T04 构建日线、指数、行业、估值、公司行为和 PIT 组件版本。
- [ ] M3-T05 实现日频 snapshot builder 和必需组件质量门禁。
- [ ] M3-T06 为 PE 等估值保存原始快照或 PIT 计算血缘。
- [ ] M3-T07 实现策略字段依赖：required、optional、display-only。
- [ ] M3-T08 实现版本化降级政策；无政策时缺失字段失败关闭。
- [ ] M3-T09 与当前 `SelectionPanelV2` 输出做逐日、逐字段对比。
- [ ] M3-T10 生成数据覆盖、缺失、延迟和来源审计报告。

### 9.3 自测

- 同一交易日重复采集不覆盖原始事实，不产生重复 snapshot；
- 修改任一必需组件，日频 snapshot ID 变化；
- 必需组件缺失时不能进入 `COMMITTED`；
- 缺 PE 时不会临时移除估值过滤或将其填零；
- 复权信号价格和原始成交价格没有混用；
- 历史 PIT 覆盖不足时相关历史决策失败关闭；
- 同一 committed snapshot 构建两次因子结果哈希一致。

### 9.4 退出条件

- 日频数据服务至少连续 5 个有效交易日以 shadow 方式运行；
- 5 日内必需日频快照和关键作业在允许重试后成功率为 100%，并在配置的
  `daily_ready_deadline` 前提交；
- 新旧入口无未解释差异；已解释差异必须具有原因码和审计记录；
- 不为服务重启设置人为次数上限；任一次重启只有在恢复后未遗漏快照、未超过截止时间时
  才不打断观察，否则 5 日窗口重新计数；
- 现有策略尚未切换写入来源；
- M3 Review 结论为 `DAILY_DATA_SHADOW_ACCEPTED`。

## 10. M4：策略注册、多账户和黄金结果适配

### 10.1 目标

建立稳定账户命名空间和策略插件，将 VCP、Alpha158 接入独立 shadow 账户。

### 10.2 任务

- [x] M4-T01 实现 `strategy_instances`、`strategy_activations` 和配置哈希。
- [x] M4-T02 实现稳定 `account_id`，禁止由 config hash 推导。
- [x] M4-T03 扩展交易 envelope，使订单、成交和风险决策显式携带账户身份。
- [x] M4-T04 实现 `logical_trades`、position lots 和管理政策归属。
- [x] M4-T05 实现 activation 切换和逐笔 `PositionManagementTakenOver`。
- [x] M4-T06 实现 `StrategyPlugin` 和只读 `AccountView`。
- [x] M4-T07 编写 VCP 适配器，不改变信号和退出时序。
- [x] M4-T08 编写 Alpha158 适配器，不改变现有 shadow 规则。
- [x] M4-T09 将现有 `PortfolioExecutor` 包装为可测试领域计算核心。
- [ ] M4-T10 创建两个以上相同或不同策略配置的独立 shadow 账户。

### 10.3 自测

- 相同策略、相同配置的两个实例拥有不同账户和无冲突订单；
- 配置升级后账户身份不变，新开仓使用新 activation；
- 旧持仓默认继续使用旧退出政策；
- takeover 后只有被明确接管的 trade 使用新政策；
- 不同管理政策的 lot 不会被不可逆聚合；
- VCP 和 Alpha158 在同一数据快照下与黄金候选、意图、审批和日线结果一致；
- 单个策略异常不修改任何账户，也不影响其他策略运行。

### 10.4 退出条件

- 至少两个 shadow 账户共享日频快照且账户状态完全隔离；
- VCP、Alpha158 黄金结果无未解释差异；
- M4 Review 结论为 `MULTI_ACCOUNT_DAILY_SHADOW_ACCEPTED`。

## 11. M5：共享分钟行情中心

### 11.1 目标

所有策略共享一次分钟采集，并获得固定 revision、可增量消费的跨证券快照。

### 11.2 任务

- [x] M5-T01 汇总持仓、待执行订单、候选、基准和哨兵证券为动态 watchlist。
- [x] M5-T02 固定分钟 stream 为 `market-minute:{trading_session}:{interval_minutes}`；
  为 watchlist 生成内容 ID、单调版本和变更原因，但不切换 stream。
- [x] M5-T03 实现批量、限流、超时 SLA 分类和供应商语义兼容检查。v1 不在线程内
  强杀同步 AKShare 调用；底层请求返回后若超过 SLA 则失败关闭，并在自然运行中观察阻塞风险。
- [x] M5-T04 继续使用 `MinuteBarStore` 保存不可变证券分区和修订。
- [x] M5-T05 实现 `available_at <= decision_cutoff` 的 revision 选择政策。
- [x] M5-T06 生成 selected event ID 列表和 `selected_bar_set_sha256`。
- [x] M5-T07 构建跨证券 snapshot、交易日流连续 sequence 和 previous snapshot，确保
  watchlist 变化前后仍连续。
- [x] M5-T08 实现账户/执行器的增量快照消费 API。
- [x] M5-T09 为迟到修订生成审计事件，不重新驱动历史状态。
- [x] M5-T10 改造现有 shadow runner，服务路径只消费增量快照并持久化状态机；旧入口
  继续保留用于冻结基线兼容。
- [x] M5-T11 输出覆盖率、延迟、revision、gap 和供应商切换指标。

### 11.3 自测与故障注入

- 50 只证券采集到第 24 只退出：不发布半成品跨证券 snapshot；
- 同一 09:35 Bar 在 09:40 修订：09:36 snapshot 仍固定原 revision；
- 两个账户消费同一 `snapshot_id` 和 selected event set；
- 账户已经成交后出现旧 Bar 修订：成交不变，仅增加审计记录；
- snapshot sequence gap：执行器等待并停止推进；
- W1 变为 W2 后 snapshot sequence 继续递增，W1 尾部丢失时 W2 不能掩盖 gap；
- 新增证券只从加入版本后参与，删除证券不会移除持仓、有效订单或未终结执行状态；
- 午休不生成虚假 Bar；
- 数据源切换字段不兼容时该证券状态失败关闭；
- watchlist 变化不会让旧 snapshot 的证券集合发生变化。

### 11.4 退出条件

- M5 软件链路通过，但状态保持 `ACCEPT_DATA_ONLY`；
- 开始积累至少 10 个有效交易日的延迟和覆盖样本；
- M5 Review 结论为 `MINUTE_DATA_ONLY_ACCEPTED`。

## 12. M6：事务模拟券商和分钟买入执行

### 12.1 目标

将账户变更、消费记录、领域事件和通知 outbox 放进统一事务，并接入冻结的分钟买入政策。

### 12.2 任务

- [x] M6-T01 实现账户版本、串行命令队列和事务仓储。
- [x] M6-T02 将现金、持仓、订单、预留、成交和风险决策映射到 SQLite。
- [x] M6-T03 实现账户事件事务模板和 `processed_events` 幂等检查。
- [x] M6-T04 在同一事务写入账户变化、stream watermark、domain event 和 notification outbox。
- [x] M6-T05 将 `IntradayOrderMachine` 改为持久化增量状态转换，不重算全部历史 Bar。
- [x] M6-T06 接入 `hybrid_intraday_entry_v1`，只允许分钟买入执行。
- [x] M6-T07 保持既有日线卖出、T+1、公司行为应收、费用、滑点、停牌和跌停延期语义；
  日线退出仍由收盘后生成订单、后续交易日开盘执行，不新增分钟卖出入口。
- [x] M6-T08 实现 `account_sessions` 和交易日阶段状态机。
- [x] M6-T09 构建单一 `preopen_snapshot`，固定公司行为、证券状态、价格限制、应收截止和
  待执行订单；缺少任一必需输入时不进入 `PREOPEN_INPUTS_READY`。
- [x] M6-T10 分别实现应收/公司行为应用、开盘卖出和开启分钟买入的幂等阶段转换。
- [x] M6-T11 要求分钟买入事务校验 `INTRADAY_BUYS_ENABLED`。
- [ ] M6-T12 实现日终核算、阶段完成和账户对账。
- [ ] M6-T13 实现账户、订单、成交和持仓的只读投影导出。
- [ ] M6-T14 建立独立组合回放和 D0/M1/M2 配对比较入口。

### 12.3 故障注入

在以下边界强制退出并恢复：

- 写订单后、成交前；
- 写成交后、持仓前；
- 更新持仓后、processed event 前；
- processed event 后、domain event 前；
- domain event 后、notification outbox 前；
- outbox 后、COMMIT 前；
- COMMIT 后、调用方收到结果前。
- 每个账户日阶段提交后、下一阶段开始前；
- 分钟快照已到达但盘前输入尚未 ready；
- 应收已应用后进程退出并重启；
- 开盘卖出已提交后进程退出并重启。

每个场景都必须证明：

- 账户要么完全未变化，要么完整变化；
- 相同事件重放不重复订单和成交；
- 已提交成交一定存在通知 outbox；
- 未提交成交不存在通知 outbox；
- stream watermark 与账户事实一致。

### 12.4 功能自测

- T+1、费用、滑点、涨跌停、停牌和公司行为保持现有语义；
- v1 没有分钟卖出、动态加仓或减仓入口；
- 未达到 `INTRADAY_BUYS_ENABLED` 时，任何分钟事件都不能产生买入成交；
- 重启不会重复应用应收、公司行为或开盘卖出；
- 两个账户同时消费同一快照但独立审批和成交；
- 一个账户资金不足不影响另一个账户；
- D0 黄金结果保持一致；
- M1/M2 结果可用固定 snapshot 和状态转换序列确定性重放。

### 12.5 退出条件

- 只运行独立 shadow 账户；
- 事务、故障注入和独立组合回放均通过；
- 分钟数据自然时间门禁尚未满足时，不得接管现有模拟账户；
- M6 Review 结论为 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED`。

### 12.6 MOCK 与真实数据双门禁

M6 软件开发不等待自然时间，可以使用合成事件、固定 snapshot 和故障注入完成
`MOCK_SOFTWARE_ACCEPTED`。MOCK 验收至少覆盖正常成交、无成交、缺 Bar、乱序、revision、
stream gap、涨停、停牌、容量限制、阶段提前到达、重复事件、事务中断和重启恢复。

`MOCK_SOFTWARE_ACCEPTED` 只证明代码在已建模场景中满足契约，不能升级为
`MINUTE_DATA_ONLY_ACCEPTED` 或 `TRANSACTIONAL_PAPER_SHADOW_ACCEPTED`。真实数据仍必须验证供应商
延迟、覆盖率、字段漂移、真实修订、断流、停牌和数据源切换；通过 M5 自然时间门禁后，才能
让独立 shadow 账户消费实时分钟 snapshot。

## 13. M7：企业微信通知和交易K线图

### 13.1 目标

实现不影响交易事务、允许重复且不会静默丢失的持久化通知。

### 13.2 任务

- [ ] M7-T01 实现 notification event 和 TEXT/CHART_IMAGE 分片。
- [ ] M7-T02 实现图表渲染任务，标记信号、委托、成交、止损和原因。
- [ ] M7-T03 实现分片状态机和独立重试。
- [ ] M7-T04 实现 `UNKNOWN -> RETRY_PENDING`。
- [ ] M7-T05 实现带抖动指数退避和最大退避间隔。
- [ ] M7-T06 达到自动尝试阈值后进入 `REQUIRES_ATTENTION`。
- [ ] M7-T07 实现人工 retry/abandon CLI，并记录操作者、原因和时间。
- [ ] M7-T08 企业微信消息显示短事件 ID，便于识别重复。
- [ ] M7-T09 迁移现有 bot 或建立受控桥接，不允许先发网络消息再创建 outbox。
- [ ] M7-T10 对 webhook、token、用户 ID 和日志执行脱敏检查。

### 13.3 自测与故障注入

- 成交事务提交后、工作器读取前退出：恢复后仍发送；
- 远端成功、本地确认前退出：进入 UNKNOWN 并重试，重复可观测；
- 文字成功、图片失败：只重试图片；
- 永久权限错误：进入 `REQUIRES_ATTENTION`，不自动删除；
- 人工 abandon 后保留完整审计记录；
- 通知失败不回滚交易、不阻塞其他账户；
- 日志和产物中找不到实际密钥。

### 13.4 退出条件

- 文本和图片均可独立恢复；
- 文档和页面明确说明可能重复以及不保证外部必然送达；
- M7 Review 结论为 `NOTIFICATION_PIPELINE_ACCEPTED`。

## 14. M8：可视化、对账和运维

### 14.1 目标

提供只读运行视图，并完成持续运维所需的对账、备份和恢复流程。

### 14.2 任务

- [ ] M8-T01 实现只读 `DashboardQuery`，禁止通过页面写账户。
- [ ] M8-T02 复用现有可视化生成统一静态首页和账户页面。
- [ ] M8-T03 展示系统健康、作业、数据覆盖、延迟和 snapshot 水位线。
- [ ] M8-T04 展示市场、行业、个股、因子和数据可得时间。
- [ ] M8-T05 展示多账户净值、回撤、成本、仓位和策略版本。
- [ ] M8-T06 展示订单、成交、原因、风险审批和交易K线图。
- [ ] M8-T07 实现每日资金、订单、预留、持仓、lot 和账户事件对账。
- [ ] M8-T08 实现数据 manifest、数据库引用和孤儿文件审计。
- [ ] M8-T09 编写运行、故障恢复、备份和切换 runbook。
- [ ] M8-T10 完成空目录完整恢复演练并保存证据。
- [ ] M8-T11 实现磁盘 warning/critical 监控；只自动清理 `REBUILDABLE` 数据。
- [ ] M8-T12 实现原始分钟数据和供应商响应的审计式备份归档，不自动删除权威事实。

### 14.3 自测

- 删除所有派生 CSV/HTML/PNG 后可以完整重建；
- 只读页面无法取得数据库写权限；
- 任一成交可以追溯到行情 snapshot、strategy activation、风险决策和输入事件；
- 任一持仓 lot 可以追溯到开仓和管理 activation；
- 对账故意注入一股或一分钱差异时能够失败并阻断完成状态；
- backup manifest 缺文件或哈希错误时恢复失败关闭；
- warning 水位暂停非必要渲染和回补，critical 水位阻止新快照和账户变化；
- 页面故障不影响调度、采集和账户事务。

### 14.4 退出条件

- shadow 环境至少连续运行 10 个有效交易日；
- 10 日内每日账户、订单、持仓、lot 和水位线对账 100% 通过；
- 关键作业在允许重试后成功率 100%，且没有未解释差异；
- 至少完成 1 次受控服务重启和 1 次数据库加不可变文件的完整恢复演练；
- 不为非计划重启设置人为次数上限；任一次非计划重启只有在未遗漏关键作业、未产生 gap
  且在下一个关键截止时间前恢复时才不打断观察，否则 10 日窗口重新计数；
- 运维人员可以仅按 runbook 完成停止、恢复和备份回滚；
- M8 Review 结论为 `OPERATIONS_READY_FOR_ADMISSION`。

## 15. M9：自然时间准入和正式模拟账户切换

### 15.1 分钟政策准入

必须继承现有门禁：

1. 至少 10 个有效交易日采集与校准；
2. 冻结覆盖率、延迟、revision 和质量阈值 manifest；
3. 阈值冻结后重新连续观察至少 20 个有效交易日；
4. 使用实际归档数据运行 D0/M1/M2 正式配对评估；
5. 运行各执行政策的独立组合回测；
6. 完成迟到修订、gap、源切换和中断统计；
7. 重新执行独立准入 Review。

未完成前保持 `ACCEPT_DATA_ONLY`，不得通过调整日期、删除异常日或沿用冻结前样本绕过。

### 15.2 调度入口迁移

作业按所有权矩阵逐项迁移，不使用“日频调度整体已迁移”状态。至少分别管理日频数据、
因子构建、策略 shadow、正式 D0 账户推进、分钟数据、分钟执行 shadow、通知和报告。
旧脚本若把多类作业绑定在一次运行中，在拆分或增加可审计阶段开关之前按一个组合 owner
处理，不做部分切换。
某个作业可以早于分钟政策准入迁移，但必须满足：

- 新旧入口使用同一输入时黄金结果一致；
- 新服务已通过作业恢复、快照事件恢复和备份恢复；
- 正式账户仍只有旧入口写入；
- 新服务使用独立 shadow 账户；
- 该 `job_id` 已记录新旧 owner、cutover session、shadow 截止、旧调度禁用时间、禁用证据
  和回滚边界；
- 只停止已被同类新作业接管的 Codex 自动任务，不连带停止其他任务。

### 15.3 正式模拟账户切换任务

- [ ] M9-T01 选择交易日边界和维护窗口。
- [ ] M9-T02 冻结逐作业所有权矩阵，停止旧写入者并保存不可自动恢复写入的证据。
- [ ] M9-T03 导出旧账户状态、配置、代码和数据哈希。
- [ ] M9-T04 导入新数据库并生成含 `cutover_epoch` 的切换事件。
- [ ] M9-T05 对账现金、持仓、lot、订单、预留、应收和最后交易日。
- [ ] M9-T06 在旧状态目录写入 cutover marker，使旧脚本和自动任务拒绝写入。
- [ ] M9-T07 启动新写入者并验证 OS 文件锁。
- [ ] M9-T08 记录首个新系统账户事件 ID 和 `account_version`。
- [ ] M9-T09 运行首日只读预检查和首个交易日增强监控。
- [ ] M9-T10 在首次新事件提交前验证可恢复旧写入者；提交后只允许向前修复、使用旧程序
  读取最新数据库，或经过独立评审的最新状态反向迁移。
- [ ] M9-T11 测试切换后旧脚本、旧 Codex 自动任务和手工命令意外启动均被拒绝。

### 15.4 完成条件

- 准入 Review 明确给出 `ACCEPT_PAPER_EXECUTION`；
- 正式模拟账户任一时刻只有一个写入者；
- 切换前后账户状态和事件水位线完全对账；
- 首次新系统提交后不会恢复切换前旧快照；
- 切换后至少连续 5 个有效交易日无重复订单、重复成交和未解释资金差异；
- 5 日内任何账户对账失败或旧写入者成功写入，观察窗口重新计数并停止新买入；
- M9 Review 记录实际切换或继续保持 shadow 的结论。

## 16. 测试文件规划

预计新增：

```text
tests/test_service_lock.py
tests/test_operational_store.py
tests/test_schema_migration.py
tests/test_job_store.py
tests/test_job_ownership.py
tests/test_domain_event_store.py
tests/test_event_dispatcher.py
tests/test_stream_watermark.py
tests/test_event_consumer_lifecycle.py
tests/test_market_snapshot_catalog.py
tests/test_daily_market_hub.py
tests/test_minute_market_hub.py
tests/test_minute_snapshot_selection.py
tests/test_strategy_registry.py
tests/test_strategy_activation.py
tests/test_trade_management_takeover.py
tests/test_account_session_phase.py
tests/test_transactional_paper_broker.py
tests/test_incremental_intraday_execution.py
tests/test_notification_outbox.py
tests/test_notification_rendering.py
tests/test_reconciliation.py
tests/test_backup_restore.py
tests/test_retention_and_disk_guard.py
tests/test_account_cutover.py
tests/test_dashboard_queries.py
tests/test_paper_service_end_to_end.py
```

扩展现有：

```text
tests/test_minute_bar_store.py
tests/test_intraday_execution.py
tests/test_portfolio_executor.py
tests/test_vcp_paper_trading.py
tests/test_alpha_forward_shadow.py
```

测试不得访问真实网络或真实企业微信。网络连通性、真实时延和通知 smoke test 作为独立
受控运行证据，不替代确定性单元测试。

## 17. 每项工作完成模板

每个任务完成后必须记录：

```text
任务 ID
代码和配置变更
对应规格条款
运行的测试命令
测试数量和结果
故障注入场景
生成的证据路径及 SHA256
已知限制
是否影响现有状态或自动任务
Review 结论
```

一个任务只有同时满足以下条件才可勾选：

- 代码和 schema 已提交到计划范围；
- 新测试通过；
- 相关既有回归通过；
- 故障路径已验证；
- 文档和原因码已更新；
- 没有把未完成工作隐藏在默认降级路径中。

## 18. 回归测试策略

每个里程碑至少运行三层测试：

1. **目标测试**：本里程碑新增和直接修改模块；
2. **相邻回归**：行情、SelectionPanel、TradeIntent、风险、PortfolioExecutor、VCP、
   Alpha158、分钟执行和企业微信队列；
3. **完整测试**：在里程碑 Review 和提交前运行仓库完整测试集。

失败处理：

- 新失败必须修复或由明确证据证明与本工作无关；
- 既有失败必须在基线中登记，不能通过降低断言或跳过测试隐藏；
- 涉及账户、时间和数据 revision 的测试必须使用固定时钟和确定性输入；
- 不允许用真实当前时间、无种子随机数或未排序集合生成核心 ID。

## 19. 风险与控制

| 风险 | 影响 | 控制 |
| --- | --- | --- |
| SQLite 单写入吞吐 | 多账户事件排队 | v1 控制账户数量；短事务；先测量再优化 |
| 文件与数据库双存储 | 孤儿或引用损坏 | 内容先落盘、数据库发布、启动审计 |
| 行情源限流和不稳定 | 分钟缺失 | 合并 watchlist、限流、显式失败关闭 |
| 历史 PIT 不完整 | 因子污染 | required 字段门禁和前瞻积累 |
| 迟到行情修订 | 重写历史状态 | snapshot selection、序号、水位线和审计事件 |
| 配置升级 | 旧持仓无人管理 | trade 级 management activation 和 takeover |
| 企业微信结果未知 | 重复或漏送 | UNKNOWN 重试、事件编号、人工处置 |
| 旧新系统并行 | 重复写账户 | shadow 独立账户和交易日边界单写者切换 |
| 本地机器停机 | 分钟数据断档 | 明确断档，不用事后数据伪造成交 |
| 范围膨胀 | 未验证行为进入模拟盘 | v1 禁止盘中卖出和动态加减仓 |

## 20. 最终验收清单

- [ ] 不依赖 Codex 自动任务跨交易日运行；
- [ ] OS 文件锁阻止第二主实例；
- [ ] 作业每次 attempt 均有独立历史；
- [ ] 快照 COMMITTED 和 domain event 同事务提交；
- [ ] 重启后不会丢失已提交但未分发事件；
- [ ] 分钟快照固定 revision、前驱、序号和 selected set hash；
- [ ] 动态 watchlist 不会重置当日分钟 stream 或掩盖序号 gap；
- [ ] 账户只增量推进订单状态，不从头重算历史；
- [ ] 账户盘前阶段未完成时分钟买入不能成交；
- [ ] 迟到行情修订不改写既有成交；
- [ ] 两个以上模拟账户共享行情且状态隔离；
- [ ] 账户变化、消费记录、domain event 和通知 outbox 原子提交；
- [ ] 相同事件重放不重复订单和成交；
- [ ] 配置升级后旧持仓管理政策明确；
- [ ] 账户批量事件能审计全部受影响的 management activation；
- [ ] 必需数据缺失时不产生交易；
- [ ] VCP 和 Alpha158 黄金结果通过；
- [ ] v1 没有引入分钟卖出、动态加仓或减仓；
- [ ] 企业微信 UNKNOWN 会重试，文字和图片独立处理；
- [ ] 外部投递重复和人工终止均可审计；
- [ ] 可视化是只读投影；
- [ ] 每日账户和数据对账无未解释差异；
- [ ] 领域事件消费者生效、退休和归档边界明确；
- [ ] 磁盘 warning/critical 策略通过验证且不自动删除权威数据；
- [ ] SQLite 与不可变文件完整恢复演练通过；
- [ ] 分钟政策满足 10 日校准和冻结后连续 20 日观察；
- [ ] 正式模拟账户只有一个写入者；
- [ ] 每个迁移作业都有 owner、禁用证据和回滚边界；
- [ ] 首次新系统账户提交后不会恢复旧账户快照；
- [ ] 密钥没有进入仓库、日志和运行产物；
- [ ] 最终 Review 明确记录接管或继续 shadow 的决定。

## 21. 开发启动顺序

计划批准后，第一批工作只执行：

1. M0-T01 至 M0-T04：基线、状态盘点和数据库 schema；
2. M0-T05 至 M0-T07：事件、流水位线和快照 schema；
3. M0-T08 至 M0-T12：账户、activation、事务和通知状态机；
4. M0-T13 至 M0-T18：文件持久化、黄金夹具和原因码；
5. M0-T19 至 M0-T22：逐作业所有权、账户日阶段、容量和定量门禁；
6. M0-T23：完成全部自测后生成 M0 Review 结论。

M0 Review 通过前不创建常驻交易服务，不修改现有正式模拟账户，也不停止当前自动任务。
