# A 股选股研究与组合风险引擎 v2 开发计划

| 字段 | 内容 |
|---|---|
| 状态 | Implemented |
| 计划版本 | 0.1.0 |
| 创建日期 | 2026-10-02 |
| 对应 Spec | [A 股选股研究与组合风险引擎 v2 Spec](../specs/selection_research_engine_v2.md) |
| 基线提交 | `460ef76` |
| 当前阶段 | M0–M7 功能开发与分步自测已完成；正式全样本实验待运行 |

## 1. 计划目标

本计划将 v2 Spec 转换为可以顺序实施和验收的工程任务。最终交付包括：

- 冻结且可校验的数据快照；
- 无前视的 PIT 数据面板；
- 证券生命周期和保守估值；
- A 股价格限制与开盘成交模型；
- 统一订单生命周期和独立资金账；
- 可执行 placebo v2；
- 组合风险引擎及 shadow mode；
- B1/B2 风险实验；
- 独立版本的 VCP 和注意力策略；
- 市场状态、区块蒙特卡洛和完整审计报告。

开发阶段必须按依赖顺序推进。M0 至 M4 未完成前，不根据回测收益调整 VCP 参数。

## 2. 工作原则

### 2.1 兼容与修复分开

新执行器首先提供 `legacy_compat` 模式，在不触发生命周期和涨跌停修复的样本上复现旧结果。修复模式使用 `pit_corrected`，所有差异必须有原因码。

### 2.2 一次只改变一个研究变量

相邻实验只允许改变一个维度：

- 执行基础设施；
- 组合约束；
- R 仓位；
- 退出规则；
- 注意力因子。

### 2.3 先记录再拦截

组合风险引擎先以 shadow mode 运行。确认覆盖率、拒单原因和风险公式正确后，才启用缩量和拒单。

### 2.4 产物不可覆盖

每次正式运行使用独立 `experiment_id`，目录名至少包含策略版本、配置哈希和数据快照 ID。旧结果只读保留。

## 3. 目标代码结构

```text
abupy/AlphaBu/
  ABuSelectionPanelV2.py
  ABuSecurityLifecycle.py
  ABuPriceLimit.py
  ABuTradeIntent.py
  ABuPortfolioExecutor.py
  ABuPortfolioRisk.py
  ABuSelectionStrategiesV2.py
  ABuMatchedPlaceboV2.py
  ABuResearchStatistics.py

configs/selection/
  risk_v1.json
  execution_v2.json
  vcp_core_v1.json
  vcp_attention_v1.json

scripts/
  freeze_selection_snapshot.py
  audit_selection_coverage.py
  backtest_selection_v2.py
  run_placebo_v2.py
  run_selection_stress.py

tests/
  test_selection_panel_v2.py
  test_security_lifecycle.py
  test_price_limit.py
  test_portfolio_executor.py
  test_portfolio_risk.py
  test_matched_placebo_v2.py
  test_vcp_strategy.py
  test_research_statistics.py
```

旧文件 [ABuSelectionStrategies.py](../../abupy/AlphaBu/ABuSelectionStrategies.py) 保留为 v1 基线，不在原文件上继续叠加 v2 功能。

## 4. 里程碑总览

| 里程碑 | 内容 | 依赖 | 主要门禁 |
|---|---|---|---|
| M0 | 冻结基线与数据清单 | 无 | 输入可通过哈希复现 |
| M1 | PIT 面板、掩码和生命周期 | M0 | 无未来数据、退市估值可审计 |
| M2 | 价格限制、订单生命周期和执行器 | M1 | 兼容模式一致、修复差异有原因 |
| M3 | Placebo v2 | M2 | 不读取未来退出日、走统一资金账 |
| M4 | 风险引擎 shadow mode | M2 | shadow 不改变成交，风险计算可复算 |
| M5 | B1/B2 风险实验 | M3、M4 | 无影子止损、策略版本清晰 |
| M6 | VCP、事件退出和注意力消融 | M5 | 规则逐项与 Spec 一致 |
| M7 | 市场状态、蒙特卡洛和准入报告 | M6 | 统计口径和前瞻计划完整 |

实施进度记录：

| 里程碑 | 状态 | 审查记录 |
|---|---|---|
| M0 | 已完成 | [M0 Review](../reviews/selection_v2_m0_review.md) |
| M1 | 已完成 | [M1 Review](../reviews/selection_v2_m1_review.md) |
| M2 | 已完成 | [M2 Review](../reviews/selection_v2_m2_review.md) |
| M3 | 已完成 | [M3 Review](../reviews/selection_v2_m3_review.md) |
| M4 | 已完成 | [M4 Review](../reviews/selection_v2_m4_review.md) |
| M5 | 已完成 | [M5 Review](../reviews/selection_v2_m5_review.md) |
| M6 | 已完成 | [M6 Review](../reviews/selection_v2_m6_review.md) |
| M7 | 已完成 | [M7 Review](../reviews/selection_v2_m7_review.md) |

关键路径：

```text
M0 -> M1 -> M2 -> M3 -> M5 -> M6 -> M7
                  -> M4 --^
```

## 5. M0：冻结基线与数据快照

### 5.1 任务

#### M0-T1 保存基线标识

- 记录代码提交 `460ef76`；
- 记录 Python、pandas、numpy、AKShare 版本；
- 保存现有回测命令和随机种子；
- 将现有 placebo 标记为 `placebo_v1_invalid_for_inference`。

#### M0-T2 实现快照工具

新增 `scripts/freeze_selection_snapshot.py`：

- 遍历信号、原始行情和研究元数据；
- 对每个文件计算 SHA256；
- 生成排序稳定的 `snapshot_manifest.json`；
- 计算总 `snapshot_id`；
- 拒绝同一输出目录内的隐式覆盖。

#### M0-T3 实现覆盖审计

新增 `scripts/audit_selection_coverage.py`，直接扫描全部文件并输出：

- `coverage_daily.csv`；
- `coverage_symbol.csv`；
- `provider_provenance.csv`；
- `coverage_summary.json`。

字段至少包括 OHLC、成交量、成交额、流通股本、换手率、行业、ST、上市日期、退市日期和公司行为。

#### M0-T4 冻结旧结果

- 复制或登记旧 `results.csv`、曲线、交易和报告的哈希；
- 生成 `baseline_registry.json`；
- 记录哪些结论因 placebo v1 不可继续使用。

### 5.2 测试

- 相同输入两次生成相同 `snapshot_id`；
- 修改任一文件一个字节后总哈希变化；
- 文件遍历顺序不影响总哈希；
- 覆盖统计总数与研究清单一致；
- 52个明确腾讯回退文件被识别，同时统计缓存文件真实字段缺失情况。

### 5.3 完成条件

- 所有正式输入都能通过文件哈希定位；
- 基线结果和旧 placebo 状态有明确登记；
- 后续回测不能在运行中下载或更新数据。

## 6. M1：PIT 面板、掩码和证券生命周期

### 6.1 任务

#### M1-T1 建立 `SelectionPanelV2`

从 v1 读取逻辑迁移：

- 前复权 OHLCV；
- 原始 OHLCV；
- 成交额、换手率和流通市值；
- 历史行业和 ST；
- 基准指数；
- 公司行为。

新增：

- `list_date`、`delist_date`；
- `board`；
- `st_status_known`；
- `adjustment_factor`；
- 字段可用性矩阵。

#### M1-T2 构建掩码

实现并独立保存：

- `universe_mask`；
- `data_available_mask`；
- `signal_eligible_mask`；
- `buy_tradable_mask`；
- `sell_tradable_mask`；
- `breadth_denominator_mask`。

每个 false 值必须能映射到一个或多个原因码。

#### M1-T3 建立生命周期事件

新增 `ABuSecurityLifecycle.py`：

- 标准化上市、ST、停复牌、退市整理、终止上市、现金收购、换股和分红送转事件；
- 区分公告日、登记日、生效日和到账日；
- 只允许当时可见事件参与策略判断；
- 输出事件来源和完整性状态。

#### M1-T4 估值策略

实现：

- 正常收盘估值；
- 停牌最后价格与 `stale_days`；
- 终止上市零回收；
- 现金收购和换股；
- 期末1/3/5连续跌停和零回收估值。

#### M1-T5 市场广度分母

按 Spec 构建 PIT 分母，输出每日：

- 股票池数量；
- 足够历史数量；
- 长期停牌数量；
- 有效广度分母；
- MA120以上数量和比例。

### 6.2 原因码

至少包含：

```text
NOT_LISTED
AFTER_DELIST_DATE
UNKNOWN_ST_STATUS
INSUFFICIENT_HISTORY
MISSING_SIGNAL_PRICE
MISSING_RAW_PRICE
MISSING_AMOUNT
MISSING_TURNOVER
SUSPENDED
LONG_SUSPENSION
TERMINATED
```

### 6.3 测试

- 上市日前和退市日后 `universe_mask=false`；
- 修改未来退市日期不改变更早日期意图；
- 停牌不会从 universe 消失；
- 未知 ST 在主实验中被排除；
- 公司行为按登记日确认权益、按生效或到账日记账；
- 终止上市无回收时核销为零；
- 广度分母不因单日行情缺失机械下降。

### 6.4 完成条件

- 每个日期和证券的候选资格可解释；
- 旧策略命中退市、长期停牌股票的暴露已审计；
- 主样本与 ST 覆盖敏感性样本明确分离。

## 7. M2：价格限制、订单生命周期和统一执行器

### 7.1 任务

#### M2-T1 价格限制引擎

新增 `ABuPriceLimit.py`：

- 根据日期、板块、ST 和上市交易日确定限制规则；
- 使用固定精度计算涨跌停价；
- 支持历史创业板规则变化；
- 对未知规则使用5%保守 fallback；
- 输出 `limit_rule_id` 和 fallback 原因。

#### M2-T2 数据对象

新增不可变对象：

- `TradeIntent`；
- `Reservation`；
- `ApprovedOrder`；
- `Fill`；
- `Position`；
- `PositionEvent`。

对象创建后不得原地修改关键历史字段。状态变化通过新事件记录。

#### M2-T3 预审批和资金预留

- 信号日确定固定数量、最高买价和订单有效期；
- 使用最高买价预留现金和风险；
- 多订单按 `score desc, strategy_id, symbol` 固定排序；
- 订单过期释放全部预留。

#### M2-T4 开盘撮合

- 买入达到涨停或超过最高买价时拒绝；
- 卖出达到跌停时延期；
- 滑点成交价不得越过价格限制；
- 买单默认仅下一交易日有效；
- 卖单持续到成交或生命周期终止；
- 同日先处理公司行为和卖单，再处理买单。

#### M2-T5 统一资金账

- 独立复算现金、持仓数量、持仓市值和净值；
- 费用拆分为佣金、过户费、印花税和滑点；
- 保留应收现金和应收股份；
- 每日保存 accounting NAV 和保守 liquidation NAV。

#### M2-T6 兼容模式

提供：

- `legacy_compat`：用于迁移对照；
- `pit_corrected`：正式研究模式。

真实数据差异报告必须标注：生命周期、涨跌停、数量预审批、容量、期末估值或其他明确原因。

### 7.2 测试

- 主板、ST、科创板和创业板价格限制；
- 固定精度边界；
- 涨停买单拒绝、跌停卖单延期；
- 修改下一日开盘价不改变 ApprovedOrder 数量；
- 实际开盘超过最高买价时取消而非缩量；
- 资金预留、过期释放、最低佣金和100股取整；
- 日终 `cash + holdings = capital`；
- 合成无特殊事件样本与 v1 逐笔一致。

### 7.3 完成条件

- 任一成交或拒单均有确定原因；
- 兼容模式复现基线合成用例；
- 正式模式不使用未来开盘价自由决定数量；
- 真实数据差异全部可归因。

## 8. M3：可执行 Placebo v2

### 8.1 任务

#### M3-T1 原始意图时间表

策略在风险审批前输出完整候选意图。placebo 不再从已成交往返交易反推模板。

#### M3-T2 PIT 匹配池

信号日只使用：

- 当日 universe；
- 行业；
- 价格；
- 过去60日流动性；
- 流通市值；
- 历史 beta；
- 历史波动率。

禁止访问未来入场或退出状态。

#### M3-T3 替代意图

- 在匹配池中按固定种子抽样；
- 使用替代股票重新计算止损和价格上限；
- 无匹配候选时记录 `NO_MATCH`，不降低匹配要求反复抽样；
- 后续无法成交、停牌或退市均由执行器自然处理。

#### M3-T4 完整执行

每个重复拥有独立现金、订单、费用、公司行为、风险审批和期末估值。

#### M3-T5 报告

输出：

- 收益和最大回撤分布；
- actual percentile；
- 未匹配比例；
- 风险拒绝比例；
- 成交比例；
- 无法退出比例；
- 字段缺失分布。

### 8.2 测试

- 修改退出日行情不改变信号日匹配池；
- 修改未来停牌和退市状态不触发重新抽样；
- 替代组合使用自身数量、费用和滑点；
- 替代组合受现金、容量和持仓约束；
- 相同快照、配置和种子结果完全一致。

### 8.3 完成条件

- v1 percentile 从正式报告和门槛中移除；
- placebo v2 无未来污染测试通过；
- 默认1,000次重复可以断点续跑并稳定合并。

## 9. M4：组合风险引擎和 Shadow mode

### 9.1 任务

#### M4-T1 配置加载

新增 `configs/selection/risk_v1.json`，加载时：

- 严格校验字段；
- 拒绝未知字段；
- 计算配置 SHA256；
- 将完整配置复制到实验输出目录。

#### M4-T2 风险状态

每日和每次订单审批前计算：

- 净值和总市值仓位；
- 单股权重；
- 开放R；
- 行业开放风险；
- 同日已批准风险；
- 成交容量；
- 四个审批压力场景；
- 1/3/5连续跌停报告场景。

#### M4-T3 仓位求解

按 Spec 计算 R 数量，并依次应用：

1. 单笔风险；
2. 单股市值；
3. 总仓位；
4. 容量；
5. 现金；
6. 行业风险；
7. 同日风险；
8. 压力损失。

记录每一步的候选数量、缩量后数量和决定性限制。

#### M4-T4 Shadow mode

- 生成完整风险决定；
- 不修改旧策略订单；
- 对比实际成交与 shadow 批准结果；
- 汇总拒绝原因和风险暴露分布。

#### M4-T5 成交后风险复核

记录费用、滑点、公司行为导致的风险突破，并生成下一交易日降风险意图。不得回滚历史成交。

### 9.2 测试

- 单笔、组合、行业、同日、容量和压力限制独立触发；
- 多候选结果不依赖字典或线程完成顺序；
- 未知行业进入统一桶；
- 缺失 beta 使用1.50；
- shadow on/off 的成交清单一致；
- 公司行为更新开放风险，不改变历史 initial R；
- 一手风险超过预算时明确拒单。

### 9.3 完成条件

- shadow mode 不改变基线成交；
- 每个风险数字可从输出文件独立复算；
- 缺失数据导致的拒单规模已量化；
- 风险阈值尚未根据收益结果修改。

## 10. M5：B1/B2 风险实验

### 10.1 B1 实施

对旧策略增加：

- 总市值和单股市值；
- 行业市值；
- 同日新增名义金额；
- 容量；
- 基于市值的压力限制。

不增加初始止损，不报告开放R。

### 10.2 B2 实施

为旧策略创建独立的新版本和真实执行止损。每个版本必须定义：

- 初始止损公式；
- 信号空间到原始空间映射；
- 触发时点；
- 下一日无法退出处理；
- 公司行为后的当前止损更新。

### 10.3 报告

逐策略比较：

- 收益和收益保留率；
- 最大回撤和 Expected Shortfall；
- 换手与成本；
- 拒单和缩量；
- 行业集中；
- 期末无法退出；
- 风险约束的边际贡献。

### 10.4 完成条件

- B1 与 B2 不混用名称和结果；
- 旧策略的失败记录仍可访问；
- 任何 R 都对应真实可执行止损；
- 是否进入 VCP 阶段由基础设施正确性决定，不由 B1/B2 收益决定。

## 11. M6：VCP、事件退出和注意力消融

### 11.1 任务

#### M6-T1 `vcp_core_v1`

严格实现 Spec 中的：

- 趋势条件；
- `t-20...t-1` 收缩窗口；
- `t-80...t-21` 对照窗口；
- ATR 30%历史分位；
- 20日高点突破；
- 无歧义核心排序；
- 结构低点与2 ATR初始止损；
- 1 ATR最高买价；
- 8%最大计划风险距离。

#### M6-T2 固定持有期基线

先实现固定20日版本，用于实验 C 和 D。固定期满信号仍在收盘生成，下一日尝试退出。

#### M6-T3 事件退出

依次实现：

- 初始止损；
- 前5日突破失败；
- 达到1R后启用3 ATR跟踪止损；
- 市场跌破 MA200；
- 20日且 MFE 小于0.5R的停滞退出。

#### M6-T4 注意力字段

- 将换手率真正保存进面板；
- 计算振幅；
- 使用 PIT 市场广度；
- 建立共同覆盖样本；
- 输出每日和逐信号覆盖率。

#### M6-T5 消融

固定数据和执行条件，依次运行：

1. core；
2. core + 成交额；
3. 完整 attention；
4. 全样本 core 敏感性结果。

### 11.2 测试

- 所有窗口边界与 Spec 一致，突破日不进入收缩窗口；
- 修改未来数据不改变信号；
- OLS斜率、ATR分位和 percentile rank 有独立数值断言；
- 下一日跳空不改变订单数量，只影响成交或取消；
- initial R 成交后冻结；
- 移动止损只上移；
- 同日多退出原因按固定优先级标记；
- 共同样本消融使用相同证券和日期覆盖。

### 11.3 完成条件

- C、D、E、F 均可从独立配置复现；
- 没有在看到结果后修改固定窗口或阈值；
- 每项收益和回撤变化都能归因到一个实验变量；
- VCP 结果尚未越过统计门槛时保持研究状态。

## 12. M7：市场状态、蒙特卡洛和准入报告

### 12.1 市场状态

实现：

- 趋势上下状态；
- 使用历史扩展或滚动分位数的高低波动状态；
- 入场日交易归因；
- 每日 P&L 状态归因；
- 未知状态单独报告。

### 12.2 时间区块蒙特卡洛

- 完整连续日收益；
- circular block 5/10/20日；
- stationary bootstrap 平均10日；
- 每种至少5,000条路径；
- 95% Expected Shortfall；
- 回撤修复时间右删失。

### 12.3 统计报告

计算：

- 交易簇 bootstrap 置信区间；
- placebo v2 percentile；
- Benjamini-Hochberg FDR；
- 前5笔盈利贡献；
- 状态覆盖；
- accounting NAV 与 liquidation NAV；
- 数据覆盖和拒单归因。

### 12.4 前瞻登记

生成冻结的前瞻配置，记录：

- 策略和配置哈希；
- 数据需求；
- 每日运行命令；
- 异常处理；
- 最低6个月和30个独立入场日交易簇要求。

### 12.5 完成条件

- 区块蒙特卡洛只用于路径风险；
- alpha 结论只使用 placebo v2 和独立交易簇统计；
- 多重检验已经校正；
- 未达到准入门槛的策略不会进入前瞻候选；
- 前瞻期内不得根据新结果修改冻结参数。

## 13. 横向测试计划

### 13.1 每次提交必须运行

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q abupy scripts tests
git diff --check
```

### 13.2 每个里程碑必须运行

- 单元测试；
- 小型合成端到端测试；
- 固定股票小样本回测；
- 未来数据污染测试；
- 账本独立复算；
- 输出 schema 检查；
- 固定种子复现测试。

### 13.3 正式回测前必须运行

- 全量字段覆盖审计；
- 快照哈希验证；
- 生命周期异常列表；
- 无法退出持仓审计；
- placebo 小规模重复一致性；
- 风险 shadow 报告人工抽查。

## 14. 输出兼容与迁移

### 14.1 v1 保留

- 不删除 v1 脚本和结果；
- v1 报告增加“不可用于 placebo 显著性推断”标记；
- v1 策略名称不得指向 v2 实现。

### 14.2 v2 输出目录

建议格式：

```text
/Users/wjy/abu/backtests/selection_v2/
  <snapshot_id>/
    <experiment_id>/
      experiment_manifest.json
      config.json
      ...
```

### 14.3 Schema 版本

每个 CSV 或 JSON 输出包含 `schema_version`。读取器遇到未知主版本必须拒绝加载，避免静默误读旧数据。

## 15. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 上海历史 ST 不完整 | 主样本缩小或误分类 | 主实验排除未知，另做覆盖敏感性 |
| 缓存文件来源丢失 | 无法按供应商判断字段 | 直接扫描字段并恢复 provenance |
| 退市事件类型不完整 | 估值不确定 | 零回收和连续跌停情景，单独披露 |
| 日线无法模拟排队 | 突破买入偏乐观 | 涨停开盘拒绝，不模拟盘中打开 |
| Placebo 计算量大 | 运行时间增加 | 分片、断点续跑、确定性合并 |
| 组合风险规则复杂 | 难以发现公式错误 | shadow mode、逐项风险账和独立复算 |
| 参数研究者自由度 | 过拟合 | 配置哈希、实验登记、FDR和前瞻冻结 |
| 前复权历史变化 | 结果不可复现 | 冻结数据快照和信号日调整因子 |

## 16. 提交拆分建议

每个提交只完成一个可独立审查的主题：

1. `Freeze selection research snapshots`
2. `Add point-in-time selection panel masks`
3. `Model security lifecycle and conservative valuation`
4. `Add A-share price-limit execution model`
5. `Introduce intent reservation order lifecycle`
6. `Extract auditable portfolio executor`
7. `Replace matched placebo with executable v2`
8. `Add portfolio risk engine shadow mode`
9. `Run B1 and B2 risk experiments`
10. `Add frozen VCP core strategy`
11. `Add event-driven VCP exits`
12. `Add attention-factor common-sample ablations`
13. `Add regime and block-bootstrap diagnostics`

不得把基础设施修复、策略参数变化和回测结果更新混在一个提交中。

## 17. 里程碑审查模板

每个里程碑完成时记录：

```markdown
## Milestone Mx Review

- Code commit:
- Spec version:
- Snapshot ID:
- Config hash:
- Completed tasks:
- Tests run:
- Test result:
- Data coverage changes:
- Behavioral differences from v1:
- Known limitations:
- Go / no-go decision:
- Next milestone:
```

若审查结论为 no-go，后续依赖阶段不得开始。

## 18. 整体完成定义

v2 开发完成需要同时满足：

- M0 至 M7 的完成条件全部通过；
- 真实策略和 placebo 使用完全相同的风险与执行引擎；
- 不存在已知未来数据读取；
- 退市、停牌、价格限制和期末无法退出均有确定处理；
- 风险引擎支持 shadow 和 enforcement 两种模式；
- VCP 规则、退出规则和注意力因子与 Spec 一致；
- 所有正式结果绑定代码、数据、配置和随机种子；
- 旧失败记录仍可复现；
- 前瞻候选只来自通过统计准入门槛的冻结版本。

本计划完成只表示研究基础设施和候选验证流程达到约定标准，不代表策略已经具备实盘有效性。
