# Alpha158 因子全量清单（2026-10-06）

## 口径

本清单覆盖当前 Alpha158 选股项目中已经进入代码或冻结注册表的全部信号因子。
按不重复的注册项统计，共 183 个：正式模型 26 个、canonical 技术研究 130 个、
残差趋势/过热研究 5 个、BaoStock 估值研究 4 个、龙虎榜研究 7 个、财务前瞻
shadow 7 个、行业前瞻 shadow 4 个。这个数字不包含原始数据字段、模型输出
`alpha_score`、持仓状态、退出规则、
仓位和风控约束，也不代表 183 个因子被同时放入一个模型。

当前企业微信日报和 `event_exit_only` 模拟盘仍使用冻结的 26 因子
`median_imputer_plus_ridge` 模型。模型注册清单位于
`/Users/wjy/abu/shadow/alpha158_forward_v1/genesis/model_manifest.json`；其余因子均未进入
正式打分和交易。

## 已使用：冻结模型的 26 个输入因子

所有原始值只使用信号日收盘时已知数据，并在同一交易日进行横截面居中百分位排名，再交给
`Ridge(alpha=100)`。因此，下表列的是模型输入字段；`alpha_score` 是模型输出，不是第 27 个
因子。

| 因子族 | 数量 | 因子 | 含义 |
|---|---:|---|---|
| 动量与趋势 | 11 | `return_1d`, `return_5d`, `return_10d`, `return_20d`, `return_60d`, `return_120d` | 1/5/10/20/60/120 日价格收益 |
|  |  | `ma10_bias`, `ma60_bias`, `ma120_bias` | 收盘价相对 10/60/120 日均线的偏离 |
|  |  | `high_60_nearness`, `high_252_nearness` | 收盘价相对近 60/252 日最高价的位置 |
| 波动与风险 | 5 | `realized_vol_20d`, `realized_vol_60d` | 20/60 日实现波动率 |
|  |  | `downside_vol_20d` | 20 日下行收益波动率 |
|  |  | `max_drawdown_60d` | 近 60 日最大回撤 |
|  |  | `atr_fraction` | 21 日 ATR / 收盘价 |
| 成交与流动性 | 4 | `amount_ratio_20d` | 当日成交额 / 近 20 日成交额中位数 |
|  |  | `amount_cv_20d` | 近 20 日成交额变异系数 |
|  |  | `price_amount_corr_20d` | 20 日收益与成交额变化的相关性 |
|  |  | `amihud_20d` | 20 日 Amihud 非流动性指标 |
| 日内 K 线 | 3 | `intraday_return` | 收盘价 / 开盘价 - 1 |
|  |  | `close_location` | 收盘价在当日最高—最低区间的位置 |
|  |  | `range_fraction` | 当日振幅 / 前收盘价 |
| 行业相对 | 3 | `industry_return_20d` | 所属行业成分股 20 日平均收益 |
|  |  | `industry_breadth_ma60` | 所属行业中收盘价高于 60 日均线的比例 |
|  |  | `stock_excess_industry_20d` | 个股 20 日收益减行业 20 日收益 |

注意：上述 3 个行业相对因子已经在模型中使用；此前测试的“行业强度硬过滤器”是额外的
组合规则，当前没有启用，两者不能混为一谈。

## 已实现、已回测、未使用：130 个 canonical 技术因子

所有带窗口的前缀均展开为 5 个周期 `{5, 10, 20, 30, 60}`。

| 因子族 | 数量 | 完整展开规则 | 当前决定 |
|---|---:|---|---|
| 回归趋势 | 15 | `BETA{5,10,20,30,60}`, `RSQR{5,10,20,30,60}`, `RESI{5,10,20,30,60}` | 拒绝 |
| 价格位置 | 35 | `QTLU*`, `QTLD*`, `RANK*`, `RSV*`, `IMAX*`, `IMIN*`, `IMXD*`，每个前缀均取五个窗口 | 拒绝 |
| 量能结构 | 30 | `VMA*`, `VSTD*`, `WVMA*`, `VSUMP*`, `VSUMN*`, `VSUMD*`，每个前缀均取五个窗口 | 拒绝 |
| 量价持续性 | 40 | `CORR*`, `CORD*`, `CNTP*`, `CNTN*`, `CNTD*`, `SUMP*`, `SUMN*`, `SUMD*`，每个前缀均取五个窗口 | 拒绝 |
| KBar 形态 | 9 | `KMID`, `KLEN`, `KMID2`, `KUP`, `KUP2`, `KLOW`, `KLOW2`, `KSFT`, `KSFT2` | 拒绝 |
| VWAP 相对价格 | 1 | `VWAP0` | 拒绝 |

这里的 `BETA` 是价格对时间的回归斜率并按价格归一化，不是股票相对市场指数的传统 beta。
六个家族在 2020-01-01 至 2026-09-30 的统一审核中均未通过门槛：有些账户点估计提高，
但 Rank IC、Top10 增量、年度一致性和多重检验不足。统一决定为
`REJECT_ALL_FAMILIES`，不能从已看过的结果中挑一个子集重新解释。

## 已实现、已回测、未使用：其他 16 个研究因子

### 残差趋势与短期过热（5 个）

- `residual_momentum_6_1_standardized`
- `industry_excess_60d_standardized`
- `max_return_20d`
- `overheat_5d_amount`
- `gap_range_overheat`

这组因子的 Rank IC 仅小幅变化，Top10 选择能力、累计收益和常规最大回撤均变差，决定为
`REJECT_HISTORICAL_SCREEN`。

### BaoStock 估值叠加（4 个）

- `earnings_yield`：由 `peTTM` 计算的 E/P
- `book_to_price`：由 `pbMRQ` 计算的 B/P
- `sales_to_price`：由 `psTTM` 计算的 S/P
- `operating_cash_flow_yield`：由 `pcfNcfTTM` 计算的经营现金流收益率

四项等权估值分与 Alpha158 按 20%/80% 叠加的原始版和滞后 20 日版都未通过。2025 年
局部改善在 2026 年反转，组合收益下降且回撤扩大，因此不进入正式模型。失败原因是增量
不足，并非 BaoStock 字段无法取得。

### 龙虎榜事件与机构席位（7 个）

- `lhb_event_1d`
- `lhb_net_buy_ratio_1d`
- `lhb_institution_present_1d`
- `lhb_institution_net_ratio_1d`
- `lhb_institution_count_balance_1d`
- `lhb_event_frequency_20d`
- `lhb_net_buy_pressure_20d`

这组因子来自 AKShare 历史回查，并统一滞后一个交易日。它们在二阶段严格 OOS 评分中
没有提高 Rank IC，Top10 未来 20 日超额收益显著下降；25/40/60bp 三档成本下收益和
最大回撤也全部恶化，因此拒绝进入正式模型。历史回查不宣称严格 PIT，接口只继续用于
不可回填的前瞻 shadow 采集。

## 已生成、仅做前瞻观察、未使用：11 个正交因子

这些因子没有形成综合分数，不参与选股重排、过滤或交易。

### 财务质量与增长（7 个）

- `return_on_parent_equity_cumulative`
- `operating_margin_cumulative`
- `operating_cash_conversion_cumulative`
- `liability_to_asset`
- `revenue_yoy_symmetric`
- `parent_net_profit_yoy_symmetric`
- `capital_expenditure_to_operating_cash`

七项联合覆盖 5,345 只股票，占采集股票的 98.109%。由于现有数据没有完整历史修订链，
它们只能从 2026-10-06 实际采集完成时点开始做不可回填的前瞻 shadow，不能用于历史
PIT 回测或当前交易。

### 行业环境（4 个）

- `excess_return_20d_vs_market_rank`
- `breadth_above_ma20_rank`
- `breadth_above_ma60_rank`
- `amount_expansion_rank`

这四项是独立的行业环境 shadow 字段。由于历史年度稳定性未通过，当前只记录，不叠加到
正式 26 因子，也不充当行业过滤器。

### 尚未形成因子的方向

`announcement_surprise` 仍处于阻塞状态：缺少可审计的一致预期基准和完整历史修订链，
因此没有构造、没有计入 183 个注册项，也没有进入回测。

## 不应计入“当前因子”的项目

- `alpha_score`、`daily_rank`、`rank_percentile`：由因子模型生成的分数或排名。
- `holding_sessions`、`unrealized_r`、`mfe_r`、`peak_drawdown_r`、
  `stop_distance_r`、`trailing_enabled`：持仓状态变量。
- 初始止损、移动止损、停滞退出、取消排名退出：退出或执行规则。
- 单笔风险、组合开放风险、行业风险、同日新增风险、目标持仓数：仓位与风控规则。
- Top 11–20、21–30、31–50：候选排名带诊断，不是因子。
- 基本面九个采集字段：原始财务事实，用于计算 7 个财务 shadow 因子，本身不直接进入模型。
- 入场/退出 timing ML 使用的 9/13 个字段：辅助模型输入，已因跨年度不稳或截短赢家被拒绝；
  其中多数还是现有因子、模型输出或持仓状态的重复引用。
- 持仓尾部风险 ML：目前最多只保留相对风险 shadow，不触发订单，也不属于正式选股因子。

## 当前结论

正式策略只使用 26 个 Alpha158-lite 因子。其余 157 个已注册候选中，146 个已有历史回测
且未获准进入正式模型（130 个 canonical、5 个残差/过热、4 个估值、7 个龙虎榜），
11 个只做前瞻 shadow（7 个财务、4 个行业）。全因子效用实验已把具备统一严格 OOS
评分的 161 个因子按预先冻结的家族等权方法合成，组合点估计和正式准入结论应以该实验
报告为准；仍不得从已看过的结果中追加筛选权重或挑选单因子。
