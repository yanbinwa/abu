# A 股选股研究与组合风险引擎 v2 Spec

| 字段 | 内容 |
|---|---|
| 状态 | Implemented；正式全样本实验待运行 |
| Spec 版本 | 0.1.0 |
| 创建日期 | 2026-10-02 |
| 基线代码 | `460ef76` |
| 基线数据区间 | 2020-01-02 至 2026-09-30 |
| 实施原则 | 先修复研究基础设施，再开发和评估 VCP |

配套开发计划：[A 股选股研究与组合风险引擎 v2 开发计划](../plans/selection_research_engine_v2_development_plan.md)。

## 1. 背景

当前研究代码已经具备复权信号、原始价格成交、公司行为、费用、滑点、独立资金账和三类选股规则，但仍存在会影响验证结论的问题：

1. 匹配型随机组合在入场时使用了替代股票未来退出日的信息。
2. 随机组合没有使用与真实策略相同的现金、风险、成交和公司行为引擎。
3. 证券上市、退市、长期停牌和无法退出事件没有完整进入持仓估值。
4. 当前开盘成交模型不能识别不同板块和历史时期的价格限制。
5. 仓位数量在看到下一日实际开盘价后才计算，没有显式的预审批和资金预留。
6. 单笔 ATR 仓位不能控制组合开放风险、行业风险、同日新增风险和压力损失。

本 Spec 定义 v2 研究基础设施、风险引擎和波动收缩突破策略的行为。所有实现和回测报告必须以本 Spec 或后续明确版本为准。

## 2. 目标

v2 必须实现：

- 不读取决策时点之后的数据；
- 将策略信号、风险审批、订单、成交和持仓分离；
- 使用同一个执行引擎运行真实策略和匹配型随机组合；
- 显式处理证券生命周期、长期停牌、价格限制和无法退出；
- 支持组合市值、R、行业、同日新增、容量和压力损失约束；
- 保留现有策略和失败结果，不覆盖、不重命名；
- 将 VCP 规则冻结为无歧义公式后再实现；
- 输出足够完整的审计记录，使任意订单都能解释其批准、拒绝、成交和退出原因。

## 3. 非目标

v2 首期不包含：

- 实盘下单；
- 分钟或逐笔撮合；
- 根据回测结果自动搜索 VCP 参数；
- 使用 2% 或 4% 的单笔风险；
- 把时间区块蒙特卡洛解释为选股能力证明；
- 把缺失数据填成对策略有利的值；
- 北京证券交易所股票。

## 4. 强制原则

### 4.1 因果性

在交易日 `t` 收盘后生成的对象，只能访问：

- `t` 日及以前的行情；
- `t` 日及以前已经生效或已经公开的数据；
- `t` 日可确定的证券状态、行业和公司行为。

修改 `t+1` 及之后的数据不得改变 `t` 日生成的 `TradeIntent`、`Reservation` 和 `ApprovedOrder`。

### 4.2 同引擎原则

真实策略、消融策略和 placebo v2 必须经过同一套：

```text
Intent -> Reservation -> ApprovedOrder -> Fill -> Position -> ExitOrder
```

不允许为 placebo 单独使用简化收益公式。

### 4.3 失败关闭

关键字段缺失时采用以下行为：

- 无法确定上市状态：不进入候选池；
- 无法确定原始价格：不成交；
- 无法确定初始风险：不得使用 R 仓位；
- 无行业：归入统一的 `UNKNOWN` 风险桶；
- 无 beta：压力测试使用配置中的保守默认值；
- 无价格限制信息：使用配置中的保守限制，记录 `LIMIT_RULE_FALLBACK`；
- 公司行为无法确定：禁止自动推导，记录人工核查项。

### 4.4 版本化

以下对象必须携带版本：

- 数据快照；
- 策略；
- 风险定义；
- 成交模型；
- 公司行为模型；
- placebo；
- 参数配置。

任何会改变交易路径的修改必须增加版本，不得覆盖历史输出。

## 5. 数据快照与可复现性

### 5.1 快照清单

每次正式实验必须生成 `snapshot_manifest.json`，至少包含：

```json
{
  "snapshot_id": "sha256:...",
  "created_at": "2026-10-02T00:00:00+08:00",
  "start_date": 20200102,
  "end_date": 20260930,
  "code_commit": "460ef76...",
  "files": [
    {"path": "raw/sh600000.csv", "sha256": "...", "bytes": 0}
  ],
  "provider_coverage": {},
  "field_coverage": {},
  "known_limitations": []
}
```

`snapshot_id` 是按排序后的文件路径、文件 SHA256 和 Spec 版本计算的总哈希。

### 5.2 前复权冻结

前复权历史可能被未来公司行为重写。正式实验必须读取冻结快照，禁止在回测过程中重新请求或更新历史文件。

### 5.3 字段覆盖报告

必须直接扫描全部股票文件，逐日、逐证券统计：

- OHLC；
- 成交量；
- 成交额；
- 流通股本；
- 换手率；
- 行业；
- ST 状态；
- 上市和退市日期；
- 公司行为。

供应商清单只能用于追踪来源，不能代替字段覆盖检查。缓存文件必须恢复或记录原始供应商来源。

## 6. 面板数据与掩码

`SelectionPanelV2` 必须保留以下互不替代的掩码：

证券状态同时保存 `st_status` 和 `st_status_known`。正式主实验使用 `unknown_st_policy=exclude`：历史 ST 状态不确定的证券不进入主样本；另行运行包含未知状态证券的覆盖敏感性实验，不得把两者混为同一股票池。

### 6.1 `universe_mask[t, s]`

证券在日期 `t` 属于研究股票池，当且仅当：

```text
list_date[s] <= date[t]
且 date[t] <= delist_date[s]，或 delist_date 缺失且状态为上市
且证券类型属于本 Spec 支持的沪深 A 股
```

`universe_mask` 不因当日停牌或行情缺失而变为 false。

### 6.2 `data_available_mask[t, s]`

策略所需字段在 `t` 日及历史窗口内完整可用。不同策略可以定义自己的字段需求，但必须输出缺失原因。

### 6.3 `signal_eligible_mask[t, s]`

```text
universe_mask
AND data_available_mask
AND 最低历史长度满足策略要求
AND 非明确 ST
AND 价格和流动性满足策略固定规则
```

未知 ST 状态不能自动等同于非 ST，必须由配置决定拒绝或进入单独结果组。

### 6.4 `buy_tradable_mask[t, s]`

表示当日开盘订单是否允许按 v2 模型买入。它与卖出可交易状态分开保存。

### 6.5 `sell_tradable_mask[t, s]`

表示当日开盘订单是否允许按 v2 模型卖出。跌停无法卖出时必须为 false，卖单保留到下一交易日。

### 6.6 市场广度分母

日期 `t` 的市场广度分母定义为：

```text
universe_mask[t]
AND 至少具有策略规定的最小历史长度
AND 非长期停牌
```

不得以“当日有行情的股票”直接作为分母。每日必须记录分母数量、分子数量和覆盖率。

## 7. 证券生命周期与估值

### 7.1 生命周期事件

统一事件表至少支持：

- `LISTED`；
- `ST_ENTER`、`ST_EXIT`；
- `SUSPENDED`、`RESUMED`；
- `DELISTING_PERIOD_START`；
- `TERMINATED`；
- `CASH_ACQUISITION`；
- `STOCK_SWAP`；
- `CASH_DIVIDEND`；
- `STOCK_DIVIDEND`；
- `SPLIT_OR_CONSOLIDATION`。

事件必须保存公告日、生效日、登记日、现金或股份调整方式和数据来源。只有在决策日已经可见的信息可以进入策略判断。

### 7.2 日常估值

- 有有效原始收盘价：使用当日原始收盘价；
- 当日停牌：账面净值使用最后有效原始收盘价，同时增加 `stale_days`；
- 已终止上市且无现金、换股或恢复交易信息：终止上市后的首个市场交易日核销为零；
- 有现金收购：按可归属于持仓的确定现金金额记账；
- 有换股：根据生效日和换股比例变更证券及数量。

### 7.3 期末无法退出

报告同时输出：

1. `accounting_nav`：按上述日常估值；
2. `liquidation_nav_1_limit`：无法退出持仓承受1个适用跌停后的估值；
3. `liquidation_nav_3_limits`：承受3个连续适用跌停；
4. `liquidation_nav_5_limits`：承受5个连续适用跌停；
5. `zero_recovery_nav`：已进入终止上市且回收方式不明的持仓按零估值。

主报告必须列出无法退出证券、账面金额、停牌天数和各情景减值。策略准入使用 `liquidation_nav_3_limits`，并同时检查 `zero_recovery_nav`。

## 8. A 股开盘成交模型 v2

### 8.1 价格限制表

价格限制由日期、证券板块、ST 状态和上市交易日数量共同决定。首期支持：

- 沪深主板普通股票：10%；
- 明确 ST 股票：5%；
- 科创板：20%；
- 创业板自 2020-08-24 起：20%，此前10%；
- 无价格限制的上市初期交易日：标记为 `NO_DAILY_LIMIT`；
- 未能确认规则：使用5%的保守限制并记录 fallback。

涨跌停价格按前一有效原始收盘价和0.01元最小价格单位计算，使用十进制四舍五入规则。实现必须用固定精度，禁止依赖二进制浮点的默认 `round`。

### 8.2 开盘买入

买单在以下任一条件成立时拒绝：

- 停牌、成交量为零或原始开盘价缺失；
- 开盘价达到或超过当日涨停价减一个最小价格单位；
- 开盘价高于订单 `max_buy_price_raw`；
- 应用滑点后的价格超过涨停价或最高允许价格；
- 资金、容量或风险预留不足。

即使当日后来打开涨停，首期也不模拟盘中排队成交。

### 8.3 开盘卖出

卖单在以下任一条件成立时延期：

- 停牌、成交量为零或原始开盘价缺失；
- 开盘价达到或低于当日跌停价加一个最小价格单位；
- 应用滑点后的价格低于跌停价。

延期卖单默认持续有效，直至成交、公司行为清算或证券终止上市。

### 8.4 容量

订单预计成交金额不得超过信号日前20个有效交易日平均成交额的5%。成交额字段缺失时：

- 主实验拒绝订单；
- 全样本敏感性实验可以使用 `原始收盘价 × 成交量`，并单独标记。

## 9. 订单生命周期

### 9.1 `TradeIntent`

必须包含：

```text
intent_id
strategy_id
strategy_version
signal_asof
symbol
side
signal_price_adjusted
signal_price_raw
adjustment_factor_signal
initial_stop_adjusted（可空）
initial_stop_raw（可空）
score
industry_asof
required_fields
missing_fields
max_gap_atr
valid_for_sessions
r_definition_version
metadata
```

### 9.2 `Reservation`

风险预审批根据 `signal_asof` 可见数据生成：

```text
reservation_id
intent_id
reserved_cash
reserved_risk
reserved_industry_risk
reserved_same_day_risk
reserved_stress_loss
expires_on
decision
reason_codes
```

### 9.3 `ApprovedOrder`

买入订单必须冻结：

```text
quantity
max_buy_price_raw
initial_stop_adjusted
initial_stop_raw
planned_initial_r_per_share_raw
planned_initial_r_cash
valid_session
```

数量以 `max_buy_price_raw` 而非下一日实际开盘价计算，并按100股向下取整。未来开盘价变化不得改变已批准数量。

### 9.4 `Fill`

成交后保存：

```text
fill_price_raw
fee_components
slippage_cost
actual_initial_r_per_share_raw
actual_initial_r_cash
fill_or_reject_reason
```

`actual_initial_r_per_share_raw = fill_price_raw - initial_stop_raw`，成交后冻结，用于交易R统计。预留风险使用更保守的 `planned_initial_r_cash`。

### 9.5 成交后复核

成交后风险复核不得撤销已经发生的成交。若实际费用、滑点或公司行为导致约束突破：

- 记录 `POST_FILL_RISK_BREACH`；
- 禁止继续增加同类风险；
- 在下一个可交易开盘按预先定义的降风险顺序处理。

## 10. 复权、止损和公司行为

### 10.1 信号日冻结字段

```text
adjustment_factor_signal = raw_close[t] / adjusted_close[t]
initial_stop_raw = initial_stop_adjusted * adjustment_factor_signal
```

同时保存原始输入值和数据快照 ID，后续不得从可能已更新的前复权历史重新推导初始R。

### 10.2 R 的两种口径

- `initial_r_raw`：成交时冻结，之后不变，用于收益和期望统计；
- `open_risk_raw`：按当前数量、当前止损和当前原始价格计算，用于组合风险审批。

移动止损和公司行为可以改变 `open_risk_raw`，不得重写历史 `initial_r_raw`。

### 10.3 公司行为更新

送转、拆并股和换股发生时，显式更新：

- 持仓数量；
- 当前原始止损；
- 当前成本基础；
- 待收现金或股份；
- 开放风险。

所有更新必须由生命周期事件驱动，不允许仅比较前后价格猜测公司行为。

## 11. 组合风险引擎 v1

### 11.1 配置

首轮冻结研究值：

```json
{
  "risk_version": "risk_v1",
  "single_trade_risk_fraction": 0.0025,
  "portfolio_open_risk_fraction": 0.0200,
  "industry_open_risk_fraction": 0.0060,
  "same_day_new_risk_fraction": 0.0075,
  "max_symbol_weight": 0.08,
  "max_gross_exposure": 0.80,
  "max_amount_participation": 0.05,
  "max_stress_loss_fraction": 0.06,
  "missing_beta": 1.50,
  "beta_clip": [0.50, 2.00],
  "unknown_industry_bucket": "UNKNOWN",
  "unknown_st_policy": "exclude",
  "unknown_limit_fraction": 0.05,
  "buy_order_valid_sessions": 1
}
```

这些数值是研究初值，不代表实盘建议。修改任一数值必须产生新的配置哈希和实验版本。

### 11.2 R 仓位

```text
risk_budget = equity_at_signal_close × single_trade_risk_fraction
quantity_r = floor(risk_budget / planned_initial_r_per_share_raw / 100) × 100
```

最终数量取以下上限中的最小值：

- `quantity_r`；
- 单股市值上限；
- 总市值上限；
- 成交容量上限；
- 可用现金和已预留现金上限；
- 行业开放风险剩余额度；
- 同日新增风险剩余额度；
- 压力损失剩余额度。

若100股的风险已经超过剩余额度，订单拒绝。

### 11.3 压力场景

所有损失均以正数表示。每个订单加入组合后独立计算以下情景，审批损失取最大值，不把不同情景重复相加。

#### `MARKET_7`

```text
position_loss_i = market_value_i × 0.07 × clip(beta_i, 0.5, 2.0)
portfolio_loss = sum(position_loss_i)
```

beta 缺失时使用1.50。

#### `INDUSTRY_10`

逐个行业计算该行业全部持仓下跌10%的损失，取损失最大的行业。缺失行业的股票全部归入 `UNKNOWN`。

#### `MARKET_5_PLUS_INDUSTRY_5`

对全部持仓施加 `5% × beta` 市场冲击，同时对一个行业增加5%冲击；逐个行业计算并取最大值。

#### `GAP_2R`

每个有真实初始止损的持仓损失取以下较大值：

```text
current_open_risk_cash
2 × actual_initial_r_cash
```

没有真实可执行止损的持仓不允许伪造R，改用该持仓市值的10%作为本场景损失。

#### 连续跌停报告

`LIMIT_DOWN_1`、`LIMIT_DOWN_3`、`LIMIT_DOWN_5` 按证券当日适用限制复利计算。三者用于报告和尾部风险披露，首期不作为逐单6%审批阈值，否则高仓位组合会机械地全部失败。

### 11.4 审批条件

订单加入后的 `MARKET_7`、`INDUSTRY_10`、`MARKET_5_PLUS_INDUSTRY_5` 和 `GAP_2R` 最坏损失不得超过净值的6%。所有场景分别输出，最终记录决定性场景。

### 11.5 Shadow mode

风险引擎首个版本必须支持 `shadow=true`：

- 不改变订单和成交；
- 记录本应批准、缩量或拒绝的决定；
- 输出每个限制的边际贡献；
- 用于检查规则是否因缺失数据或公式错误大量拒单。

## 12. 冻结旧策略与 B1/B2 实验

### 12.1 冻结策略

以下策略保留原定义和名称：

- `residual_momentum_v1`；
- `trend_reversal_v1`；
- `trend_breakout_v1`；
- `volatility_blend_v1`。

历史输出保留，不因 v2 修复而覆盖。

### 12.2 B1

旧策略只增加：

- 总市值；
- 单股市值；
- 行业市值；
- 同日新增名义金额；
- 容量；
- 基于市值的压力场景。

B1 不增加止损，不报告伪造的开放R。

### 12.3 B2

为旧策略增加真实执行的初始止损，必须创建新策略版本，例如：

- `trend_breakout_rstop_v2`；
- `trend_reversal_rstop_v2`；
- `residual_momentum_rstop_v2`。

B2 与原策略分别报告，不得描述为相同策略仅更换仓位。

## 13. VCP 策略规范

### 13.1 版本

- 核心价格策略：`vcp_core_v1`；
- 注意力增强策略：`vcp_attention_v1`；
- 所有切片使用 Python 左闭右开语义；
- `t` 是信号日，订单最早在 `t+1` 成交。

### 13.2 最低历史

股票和基准至少有252个此前交易日数据。所有规则使用前复权行情计算，成交使用原始行情。

### 13.3 趋势条件

在 `t` 日收盘后同时满足：

```text
benchmark_close[t] > benchmark_ma200[t]
close[t] > ma120[t]
ma60[t] > ma120[t]
OLS_slope(log(ma120[t-19:t+1]), x=0..19) > 0
```

MA 使用包含 `t` 的收盘价计算。OLS 遇到任一缺失值即不满足。

### 13.4 突破前收缩

```text
contraction_window = t-20 ... t-1
control_window = t-80 ... t-21

contraction_range = max(high[t-20:t]) / min(low[t-20:t]) - 1
control_range = max(high[t-80:t-20]) / min(low[t-80:t-20]) - 1
contraction_range <= 0.65 × control_range
```

波动率条件：

```text
atr_fraction[d] = atr21[d] / close[d]
atr_fraction[t-1] <= quantile(atr_fraction[t-252:t], 0.30)
```

分位数使用线性插值，窗口包含 `t-1`，不包含突破日 `t`。

### 13.5 突破条件

```text
breakout_level = max(high[t-20:t])
close[t] > breakout_level
```

核心排序分数：

```text
breakout_strength = (close[t] - breakout_level) / atr21[t]
tightness = 1 - contraction_range / control_range
core_score = 0.60 × percentile_rank(breakout_strength)
           + 0.40 × percentile_rank(tightness)
```

横截面 percentile rank 只在当日 `signal_eligible_mask` 内计算，相同值使用股票代码升序打破平局。

### 13.6 初始止损

```text
structure_low = min(low[t-20:t])
atr_stop = close[t] - 2 × atr21[t]
initial_stop_adjusted = max(structure_low, atr_stop)
```

若止损不低于信号收盘价，拒绝信号。

### 13.7 跳空和最高买入价

```text
raw_atr_signal = atr21[t] × adjustment_factor_signal
max_buy_price_raw = min(
    signal_price_raw + raw_atr_signal,
    applicable_limit_up_price
)
```

计划每股风险：

```text
planned_initial_r_per_share_raw = max_buy_price_raw - initial_stop_raw
```

若计划风险距离超过信号日原始收盘价的8%，拒绝意图。实际开盘价高于 `max_buy_price_raw` 时取消买单，不重新计算数量。

## 14. 注意力增强规范

`vcp_attention_v1` 在与 `vcp_core_v1` 相同的数据覆盖样本上增加以下规则。

### 14.1 突破前收缩

所有窗口不包含突破日：

```text
amount_dry = median(amount[t-5:t]) / median(amount[t-20:t]) <= 0.70
turnover_dry = median(turnover[t-5:t]) / median(turnover[t-20:t]) <= 0.80
amplitude[d] = (high[d] - low[d]) / previous_close[d]
amplitude_dry = median(amplitude[t-5:t]) / median(amplitude[t-20:t]) <= 0.75
```

分母必须为正且窗口内至少90%的值有效，否则为缺失。

### 14.2 突破日扩张

```text
amount_expansion = amount[t] / median(amount[t-20:t]) >= 1.50
turnover_expansion = turnover[t] / median(turnover[t-20:t]) >= 1.25
amplitude_expansion = amplitude[t] / median(amplitude[t-20:t]) >= 1.20
```

### 14.3 市场广度

```text
breadth_ma120[t] = count(close[t] > ma120[t]) / breadth_denominator[t]
breadth_ma120[t] >= 0.55
```

### 14.4 注意力排序

在通过全部硬条件的候选中：

```text
attention_score =
    0.40 × percentile_rank(amount_expansion)
  + 0.25 × percentile_rank(turnover_expansion)
  + 0.20 × percentile_rank(amplitude_expansion)
  + 0.15 × percentile_rank(breakout_strength)
```

最终排序使用 `attention_score`，平局按股票代码升序。

### 14.5 消融样本

主消融必须在成交额、换手率、振幅和广度均可用的共同样本运行：

1. `vcp_core_v1_common_coverage`；
2. `vcp_amount_v1_common_coverage`；
3. `vcp_attention_v1_common_coverage`。

另行报告全样本核心策略。每日输出共同样本覆盖率、字段缺失数和因此拒绝的意图数。

## 15. VCP 退出规范

所有退出信号在收盘后确认，下一交易日以卖出订单进入同一成交模型。

### 15.1 初始止损

```text
close_adjusted[t] <= initial_stop_adjusted
```

### 15.2 突破失败

入场后的前5个持仓交易日内：

```text
close_adjusted[t] <= breakout_level
```

### 15.3 ATR 跟踪止损

当最大有利波动首次达到 `+1 initial R` 后启用：

```text
trailing_stop_adjusted[t] = peak_close_adjusted[t] - 3 × atr21[t]
current_stop_adjusted[t] = max(initial_stop_adjusted,
                               trailing_stop_adjusted[t])
```

当前止损只允许上移。

### 15.4 市场状态退出

```text
benchmark_close[t] < benchmark_ma200[t]
```

### 15.5 停滞退出

同时满足：

```text
holding_sessions >= 20
maximum_favorable_excursion < 0.5 initial R
```

固定20日不再是无条件退出。

### 15.6 优先级

同日多个退出条件同时触发时，原因优先级为：

```text
INITIAL_STOP
BREAKOUT_FAILURE
TRAILING_STOP
MARKET_REGIME
STAGNATION
END_OF_TEST
```

优先级只决定原因标签，不改变订单数量和成交模型。

## 16. Placebo v2

### 16.1 输入

placebo 以策略生成的原始 `TradeIntent` 时间表为输入，不以已经成交的往返交易为输入。这样不会复制由未来退出和资金路径决定的模板。

### 16.2 匹配信息

替代股票只能使用 `signal_asof` 可见数据匹配：

- 当日 `universe_mask`；
- 行业；
- 价格；
- 过去60日平均成交额；
- 流通市值；
- 使用截至信号日数据估计的 beta；
- 过去60日波动率。

严禁检查替代股票未来入场日或退出日是否有行情、是否停牌、是否退市或是否可成交。

### 16.3 执行

每个替代意图必须：

- 使用替代股票在信号日重新计算的止损和订单上限；
- 进入相同 Reservation 和风险审批；
- 使用相同费用、滑点、价格限制、公司行为和生命周期事件；
- 使用相同策略版本的退出规则；
- 独立计算数量、费用、现金和期末估值。

替代意图在未来无法成交或退出是合法结果，不能提前重新抽样。

### 16.4 重复与输出

- 默认1,000个重复，开发冒烟测试可使用20个；
- 随机种子固定并写入元数据；
- 输出未匹配意图比例、风险拒绝比例、成交比例和无法退出比例；
- `actual_return_percentile` 只允许使用 placebo v2 分布计算。

旧实现标记为 `placebo_v1_invalid_for_inference`，保留结果但不得用于准入。

## 17. 实验矩阵

| 编号 | 实验 | 目的 |
|---|---|---|
| A0 | 旧引擎、旧策略 | 冻结历史失败记录 |
| A1 | v2 兼容模式、关闭修复 | 验证执行器迁移一致性 |
| A2 | v2 修正生命周期和成交 | 量化基础设施修复影响 |
| S0 | 风险引擎 shadow mode | 验证审批逻辑和覆盖率 |
| B1 | 旧策略 + 市值/行业/容量约束 | 不改变旧策略止损定义 |
| B2 | 新版本旧策略 + 可执行止损 | 衡量R仓位和真实止损 |
| C | VCP core + 固定名义仓位 + 固定20日 | 判断核心信号质量 |
| D | VCP core + R仓位 + 固定20日 | 判断仓位贡献 |
| E | VCP core + R仓位 + 事件退出 | 判断退出贡献 |
| F | 注意力共同样本消融 | 判断注意力因子贡献 |
| G | 完整规则 + placebo v2 + 压力测试 | 稳健性评估 |

除明确的实验变量外，相邻实验必须使用相同数据、股票池、费用、随机种子和成交模型。

## 18. 市场状态分析

### 18.1 状态定义

趋势：

```text
UP: benchmark_close[t] > benchmark_ma200[t]
DOWN: 其他情况
```

波动：

```text
realized_vol20[t] = std(benchmark_return[t-19:t+1]) × sqrt(252)
threshold[t] = 过去252个已完成交易日 realized_vol20 的70%分位数
HIGH_VOL: realized_vol20[t] > threshold[t]
LOW_VOL: 其他情况
```

计算阈值时不允许使用 `t+1` 之后的数据。历史不足252个阈值观测时，状态为 `UNKNOWN`。

### 18.2 两种归因

分别报告：

1. 按入场日状态分组的交易结果；
2. 按每日状态归因的组合日 P&L。

两种口径不得合并成一个指标。

## 19. 时间区块蒙特卡洛

### 19.1 输入

- 使用未按年度重置的完整连续日净值曲线；
- 使用成本后日收益；
- 保留无持仓交易日；
- 不用于证明信号 alpha。

### 19.2 方法

- circular block bootstrap，固定区块长度5、10和20日；
- stationary bootstrap，平均区块长度10日；
- 每种方法至少5,000条路径；
- 路径长度与原始连续净值曲线一致；
- 随机种子写入实验元数据。

### 19.3 输出

- 累计收益分布；
- 最大回撤分布；
- 最长水下时间；
- 年化波动率；
- 95% Expected Shortfall；
- 期末亏损概率；
- 回撤修复时间的 Kaplan-Meier 估计。

模拟结束仍未修复的回撤按右删失处理。报告必须声明模拟路径不等于独立市场样本。

## 20. 统计准入门槛

### 20.1 最低样本

历史研究至少满足其一：

- 100笔已平仓交易；
- 40个独立入场日交易簇，并且覆盖至少3种已知市场状态。

同日产生的多个相关信号视为一个交易簇。

### 20.2 信号门槛

在双边各25bp滑点和全部费用后：

- 每笔交易R期望的95%交易簇 bootstrap 置信区间下界大于0；
- 总体收益高于 placebo v2 的90%分位数才进入候选；
- 高于95%分位数才允许进入前瞻模拟盘候选；
- 至少3个年度或等长时间区块超过 placebo 中位数；
- 前5笔盈利交易对总利润的贡献不得超过50%，否则标记集中度风险；
- `liquidation_nav_3_limits` 下仍满足组合最大回撤预算。

### 20.3 风险引擎门槛

风险引擎以风险改善为目标：

- 尾部回撤和 Expected Shortfall 必须改善；
- 不要求绝对收益机械上升；
- 必须报告收益保留比例、拒单比例和容量损失；
- 任何改善不得来自前视、减少数据覆盖或改变对照股票池。

### 20.4 多重检验

同一研究批次的策略、参数和消融版本组成一个检验族。报告原始 p 值和 Benjamini-Hochberg FDR，候选准入要求 `q <= 0.10`。

探索性版本必须登记在实验清单中，不能只报告表现最佳者。

### 20.5 前瞻模拟盘

历史数据已经被反复观察，不能再作为严格的全新留出样本。参数冻结后，从2026年10月以后的新数据开始前瞻验证。

前瞻观察至少满足：

- 持续6个月；
- 至少30个独立入场日交易簇；
- 没有数据、风控或执行规则的重大违规；
- 实际成交率、滑点和拒单原因没有显著偏离研究假设。

未满足前瞻条件前，不进入实盘资金评估。

## 21. 输出与审计文件

每次正式运行至少输出：

```text
experiment_manifest.json
config.json
coverage_daily.csv
intents.csv
reservations.csv
orders.csv
fills.csv
position_events.csv
risk_daily.csv
curve_daily.csv
trades_closed.csv
rejections.csv
unexit_positions.csv
results.json
placebo_summary.csv
placebo_distribution.csv
regime_entry_attribution.csv
regime_daily_attribution.csv
monte_carlo_summary.csv
```

`experiment_manifest.json` 必须记录代码提交、数据快照、Spec 版本、配置哈希、随机种子、命令行和运行环境。

## 22. 测试要求

### 22.1 因果性测试

- 修改未来行情不得改变历史意图；
- 修改下一日开盘价不得改变已批准数量；
- 修改退出日行情不得改变 placebo 候选池；
- 市场状态阈值不得因未来数据改变。

### 22.2 生命周期测试

- 上市前不可进入 universe；
- 退市后不可新增持仓；
- 停牌期间估值、风险和卖单延期正确；
- 终止上市无回收时按规则核销；
- 现金收购和换股正确改变现金或证券；
- 期末无法退出报告包含1/3/5跌停和零回收情景。

### 22.3 成交测试

- 主板、ST、科创板和创业板历史限制正确；
- 涨停开盘买单拒绝；
- 跌停开盘卖单延期；
- 滑点价格不得穿过涨跌停；
- 买单过期，卖单持续；
- 100股取整、最低佣金、印花税和过户费正确。

### 22.4 风险测试

- 单笔、组合、行业、同日和容量约束独立触发；
- 多订单同日审批按固定排序，不依赖容器遍历顺序；
- 未知行业统一进入 `UNKNOWN`；
- beta 缺失使用保守默认值；
- shadow mode 不改变成交；
- 公司行为后开放风险更新，初始R不变。

### 22.5 Placebo 测试

- 候选池不读取未来退出日；
- placebo 独立计算股数、费用和滑点；
- placebo 走完整现金和风险约束；
- 未来无法退出的替代股票不会被重新抽样；
- 固定种子结果可复现。

### 22.6 迁移一致性

v2 兼容模式在不含退市、停牌、涨跌停和公司行为的合成样本上，必须与旧引擎逐笔一致。真实数据上的差异必须逐笔归因到明确的修复项。

## 23. 建议代码结构

```text
abupy/AlphaBu/
  ABuSelectionPanelV2.py
  ABuTradeIntent.py
  ABuPortfolioRisk.py
  ABuPortfolioExecutor.py
  ABuSecurityLifecycle.py
  ABuPriceLimit.py
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
```

旧文件 `ABuSelectionStrategies.py` 继续保留，用于复现 v1 结果和迁移对照。

## 24. 实施顺序与完成条件

### 阶段0：冻结

- 保存当前代码提交、数据快照哈希和旧结果；
- 标记 placebo v1 不可用于统计推断；
- 完成条件：所有输入均可通过哈希复现。

### 阶段1：数据和生命周期

- 增加三类核心 mask、证券事件、字段覆盖率和保守期末估值；
- 完成条件：生命周期与覆盖测试全部通过。

### 阶段2：价格限制和执行器

- 实现买卖方向不同的开盘成交规则；
- 抽取统一资金账；
- 完成条件：兼容模式通过，真实差异均有原因码。

### 阶段3：Placebo v2

- 只使用信号日信息抽样；
- 使用统一执行器；
- 完成条件：未来数据污染测试通过，v2 分布可复现。

### 阶段4：订单生命周期和风险 shadow mode

- 实现 Intent、Reservation、Order、Fill、Position；
- 输出所有风险决策；
- 完成条件：下一日价格变化不改变已批准数量。

### 阶段5：B1/B2

- 分开运行无止损约束和新增可执行止损版本；
- 完成条件：所有结果使用明确策略版本，R 只来自真实止损。

### 阶段6：VCP 与消融

- 严格按第13至15节实现；
- 依次运行 C、D、E、F；
- 完成条件：参数和股票池差异均可追踪。

### 阶段7：稳健性与前瞻

- 运行市场状态、区块蒙特卡洛、placebo v2 和压力场景；
- 冻结最终候选；
- 从2026年10月后的新数据启动前瞻模拟盘。

## 25. 决策记录

实现过程中若需要修改本 Spec：

1. 新建 ADR 或在本节追加决策；
2. 说明问题、备选方案、最终选择和对历史实验的影响；
3. 增加 Spec 版本；
4. 已运行的结果不回写、不覆盖。

在阶段3完成前，不使用随机组合分位数作为策略准入依据。在阶段5完成前，不开始根据收益调试 VCP。在阶段7和前瞻验证完成前，不评估实盘资金配置。
