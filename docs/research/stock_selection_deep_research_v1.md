# 股票选股策略深度调研与优化建议 v1

| 项目 | 内容 |
|---|---|
| 调研日期 | 2026-10-03 |
| 研究对象 | 当前 A 股 `vcp_residual_v2` 及后续选股研究体系 |
| 结论性质 | 外部证据与现有回测诊断形成的研究优先级，不代表新策略已通过回测 |
| 当前历史样本 | 2022-01-04 至 2026-09-30；首笔成交在 2023 年 |

## 1. 结论摘要

当前系统最值得优先解决的是**候选股票排序和突破质量识别**。现有风险、执行和退出框架可以继续复用。新增市场、行业和龙头字段没有带来收益，主要原因不是“市场和行业无效”，而是当前实现把少数上下文字段直接用于硬过滤或单层排序，既没有处理非线性，也没有与个股形态、流动性和预测周期对齐。

推荐按以下顺序开展研究：

1. **VCP 突破质量排序 `vcp_quality_rank_v1`**：保持候选资格、风险和退出不变，只重建排序；先预测 5 日假突破，再预测 20 日收益或实现 R。
2. **一日跟随确认 `vcp_followthrough_v1`**：VCP 信号后观察一个完整交易日，确认价格仍在突破位上方，再于下一日开盘执行。
3. **宽股票池横截面策略 `alpha158_lite_v1`**：从全体可交易股票中，用精简的价格、波动、流动性和趋势特征做横截面排序，形成独立于 VCP 的第二种 alpha 来源。
4. **注意力过热惩罚**：将成交额、换手、涨停家数、炸板率和题材拥挤度作为连续或非线性特征，检验极端关注是否应减分；不再假定“越热越好”。
5. **公告后漂移策略**：取得严格 PIT 的公告日、业绩预告和财务数据后，研究 PEAD。它与纯技术突破的相关性可能较低，但当前数据不足，暂不能回测。

暂不优先投入深度学习、强化学习、自动参数搜索或复杂行业图模型。当前只有 156 至 157 个可用横截面信号日、87 个实际入场日簇，复杂模型很容易把市场状态和个别年度记住。

## 2. 当前系统的实证诊断

现有诊断已经给出明确证据：

- 3,153 个原始可交易意图最终形成 137 笔闭合交易、87 个入场日簇；
- 当前高分成交相对同日候选的 20 日收益提升只有 `+0.21%`，95% 区间 `[-0.64%, +1.14%]`；
- 当前综合分数的同日 20 日 Spearman IC 为 `-0.0812`，95% 区间 `[-0.1354, -0.0292]`；
- 66 笔 5 日假突破交易合计亏损 79,149 元，71 笔非假突破交易合计盈利 88,726 元；
- 追踪止盈组平均 `+2.362R`，说明退出框架能够保留趋势赢家；主要损失来自入场后很快失败，而不是盈利交易过早退出。

因此，继续缩紧止损或增加组合限制，最多改善尾部风险，不能创造缺失的选股优势。现有结论详见 [VCP 入场区分度诊断](../reviews/vcp_entry_quality_diagnostic_v1.md) 和 [新增因子稳健性验证](../reviews/vcp_factor_robustness_v1.md)。

### 2.1 当前分数的结构性问题

当前 `residual_core` 将残差动量、MA120 斜率、收缩紧度和突破强度等权排名。四项分别覆盖约 6 至 1 个月、120 日趋势、20 日结构和当日突破，预测周期没有统一。实现见 [ABuVCPStrategy.py](../../abupy/AlphaBu/ABuVCPStrategy.py#L228-L319)。

此外，当前残差动量是残差收益简单求和，没有除以残差波动率。原始 residual momentum 研究使用风险标准化后的残差表现，并使用更长估计窗口。当前字段更准确的名称应是“短窗口市场残差累计收益”，不应把论文结论直接迁移到它上面。[Residual Momentum 论文](https://repub.eur.nl/pub/22252/ResidualMomentum-2011.pdf)

### 2.2 上下文实验失败不等于上下文无效

当前市场覆盖使用离散状态和风险倍率，行业层主要使用单个 20 日超额收益排名，龙头层使用与原分数高度重合的残差动量排名。这些实验否定的是**当前字段、方向和组合方式**。它们没有检验：

- 行业参与度、行业内上涨比例、创新高比例和行业扩散速度；
- 个股相对行业的 5/20/60 日超额收益；
- 市场状态与个股特征之间的有限交互；
- 注意力处于中间区间与极端拥挤区间时的不同效果。

## 3. 开源项目能复用的部分

### 3.1 Microsoft Qlib：最值得借鉴的研究范式

Qlib 的 Alpha158 不是一个神奇策略，而是一套可复现的横截面研究基线：K 线形态、价格位置、趋势、波动、成交量和滚动统计构成特征集；模型输出通过 IC、Rank IC 和成本后组合共同评价。其官方实现包含 ROC、均线、波动、回归斜率、趋势线性度、区间位置、价量相关和上涨天数比例等特征。[Alpha158 特征实现](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)

官方 LightGBM 示例明确分离训练、验证和测试期，并配置涨跌停、手续费和最低费用。[Alpha158 LightGBM 配置](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml) Qlib 还特别说明，中国日线标签必须考虑收盘后产生信号、次日才能成交的时序。[Qlib 数据与标签文档](https://github.com/microsoft/qlib/blob/main/docs/component/data.rst)

可借鉴：

- 以横截面分数作为策略与组合之间的稳定接口；
- 特征、标签、训练期和模型版本全部冻结；
- 同时看 Rank IC、ICIR 和成本后组合，而不是只看总收益；
- 用 rolling/expanding 任务生成器做时间滚动训练。[Qlib RollingGen](https://github.com/microsoft/qlib/blob/main/qlib/workflow/task/gen.py)
- 用 `top-k + drop buffer` 控制换手，而不是每期全部替换。[TopkDropoutStrategy](https://github.com/microsoft/qlib/blob/main/qlib/contrib/strategy/signal_strategy.py)

不应直接照搬 Qlib 公布的收益数字。它的股票池、数据版本、成交价和成本假设与本项目不同；官方 benchmark 也提示随机模型需要多次运行并报告波动。[Qlib benchmarks](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)

### 3.2 QuantConnect LEAN：验证当前分层架构

LEAN 将 Universe Selection、Alpha、Portfolio Construction、Execution 和 Risk Management 分开。[LEAN `QCAlgorithm`](https://github.com/QuantConnect/Lean/blob/master/Algorithm/QCAlgorithm.cs) 这与当前项目的 `SelectionPanelV2 → TradeIntent → Risk → Executor → Position` 一致，说明无需为选股研究重写交易账本。新增模型只需输出同样的 `TradeIntent`。

### 3.3 Freqtrade：把前视检查做成自动化门禁

Freqtrade 的 lookahead analysis 通过删除单个信号后重跑并比较结果，寻找指标和交易结果是否变化。[Freqtrade lookahead-analysis](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md) 它还列出 `shift(-1)`、全样本均值和错误 resample/merge 等常见泄漏来源。[策略开发文档](https://github.com/freqtrade/freqtrade/blob/develop/docs/strategy-customization.md)

当前项目已有 PIT 掩码，但模型特征和标签上线后仍应增加类似门禁：任意截断未来数据，不得改变截断日前的特征、分数和订单。

### 3.4 FinRL、TRA、HIST：保留为后续研究

FinRL 适合研究完整的强化学习交易流程，[FinRL benchmark](https://github.com/AI4Finance-Foundation/FinRL/blob/master/docs/source/finrl_meta/Benchmark.rst)；Qlib TRA 用于多个时间状态，[TRA](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/TRA/README.md)；HIST 用行业和隐藏概念图提取股票间共享信息。[HIST paper](https://arxiv.org/abs/2110.13716)

这些方向都需要更多独立市场周期、稳定的概念历史和更严格的模型选择预算。当前样本不足以证明它们优于简单模型，因此不进入前两阶段。

## 4. 论文证据对策略设计的约束

### 4.1 A 股预测中流动性变量很重要

针对中国股票市场的机器学习研究发现，流动性水平及其波动、零交易、非流动性、波动和部分基本面字段是重要预测变量；模型表现也会受到系统性状态变化影响。[Machine learning in the Chinese stock market](https://www.sciencedirect.com/science/article/pii/S0304405X21003743)

这支持把成交额、换手率、成交稳定性、Amihud 非流动性和容量纳入特征，但不支持把当日放量简单解释为利好。

### 4.2 A 股动量并不稳定，短周期反转需要单独建模

中国市场研究报告过短周期反转，并发现横截面和时间序列动量在一些设定下不显著或为负。[Momentum or contrarian trading strategy](https://www.sciencedirect.com/science/article/pii/S1059056018301928)、[Cross-sectional and time-series momentum returns: Is China different?](https://doi.org/10.1016/j.pacfin.2020.101458)

更新的 A 股显著性研究还发现，高显著性股票更容易出现由过度反应驱动的短期反转。[Salience and return reversals: Evidence from China](https://www.sciencedirect.com/science/article/pii/S0927538X25003749)

因此，突破日的高振幅、高换手、强跳空和涨停关注度应允许出现倒 U 型或阈值效应。注意力字段应作为“确认”和“过热”两种可能并存的特征，而不是固定正权重。

### 4.3 机器学习适合处理交互，但模型复杂度必须受样本约束

大样本资产定价研究显示，树模型和神经网络可以捕获动量、流动性和波动等变量之间的非线性关系。[Gu, Kelly, Xiu](https://www.nber.org/papers/w25398) 但本项目只有约 157 个有效信号日，股票行数不能当成独立时间样本。因此首版采用正则化线性模型和浅层树模型；深度网络留到历史扩充后。

### 4.4 多次试验必须计入研究成本

金融因子研究存在严重的多重检验问题，Harvey、Liu、Zhu 建议新因子使用高于传统 `t=2` 的门槛。[Cross-Section of Expected Returns](https://www.nber.org/papers/w20592) Deflated Sharpe Ratio 则用于校正试验选择、非正态和回测过拟合。[Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)

项目必须建立全局 `trial_registry`，记录所有看过结果的因子方向、阈值、标签、模型和组合版本。失败版本不能删除，也不能把同一次研究中的最优版本包装成独立样本外结果。

## 5. 推荐策略与优先级

| 优先级 | 策略/方案 | 主要目标 | 数据就绪度 | 与现有框架复用 | 当前判断 |
|---|---|---|---:|---:|---|
| P0 | `vcp_quality_rank_v1` | 降低 5 日假突破并提高同日排序 | 高 | 高 | 立即实施 |
| P0 | `vcp_followthrough_v1` | 用市场接受度确认突破 | 高 | 高 | 独立预登记 |
| P1 | `alpha158_lite_v1` | 建立非稀疏的宽股票池横截面 alpha | 中高 | 中高 | 建立基线 |
| P1 | `residual_momentum_standardized_v1` | 修正残差动量定义并统一周期 | 高 | 高 | 作为独立消融 |
| P1 | 行业参与度与相对强度 | 提供个股信号的软上下文 | 高 | 高 | 只作连续特征 |
| P2 | 注意力过热/题材扩散 | 区分确认与拥挤反转 | 低 | 中 | 等前瞻事件样本 |
| P2 | 公告后漂移 PEAD | 增加基本面事件 alpha | 低 | 中 | 先补 PIT 数据 |
| P3 | 短期流动性冲击反转 | 与趋势策略形成低相关收益源 | 中 | 高 | 单独策略研究 |
| 暂缓 | TRA/HIST/深度学习/RL | 自动学习状态和复杂关系 | 低 | 低 | 样本不足 |

## 6. P0：VCP 质量排序的精确方案

### 6.1 不改变候选资格和执行

首轮固定以下部分：

- VCP 收缩、ATR 低位、MA 趋势和突破资格；
- 次日开盘成交、最大跳空价、涨跌停和 T+1；
- 0.25% 单笔风险、组合上限、初始止损、追踪止盈和停滞退出；
- 费用和滑点；
- 当前基线失败记录。

只改变 `score`。这样结果能够回答“新排序是否挑到了更好的股票”，不会与风险参数变化混杂。

### 6.2 两阶段标签

单一 20 日收益标签会把两个不同问题混在一起。建议建立：

1. `false_breakout_5d`：按冻结定义，5 日内跌回突破位且此前未达到 `+1R`；
2. `mfe_20_r`、`mae_20_r`：未来 20 日最大有利/不利波动，按初始 R 标准化；
3. `excess_return_20d`：从真实可执行的 `t+1` 开盘到第 20 日收盘，减同期基准；
4. `realized_r`：按当前退出引擎实际得到的 R。

第一阶段用分类模型估计假突破概率；第二阶段估计收益或实现 R。组合分数使用预先冻结的期望值映射。所有标签只用于训练和评估，不能进入信号日特征。

### 6.3 首批特征

首批控制在 25 至 40 个字段，按家族整体消融：

**突破质量**

- 收盘在当日高低区间的位置；
- 实体、上影线、下影线和日内振幅；
- 收盘超过突破位的 ATR 距离；
- 当日跳空/ATR、开盘到收盘收益；
- 前高被测试次数、基础区间持续日数；
- 最近 5/10/20 日紧密收盘比例；
- 收缩区间是否呈逐级缩小，而非只有一次 20 日/60 日比值。

**趋势与风险**

- 5/10/20/60/120 日收益；
- 距 252 日高点、60 日高点的距离；
- 20/60 日回归斜率和 `R²`，区分“上涨”与“平滑上涨”；
- ATR、实现波动、下行波动、残差波动、最近最大回撤；
- 标准化残差动量：残差累计收益除以同窗口残差波动。

**流动性与注意力**

- 成交额和换手率的 20 日分位数、均值、标准差和变异系数；
- 当日成交额/换手相对 20 日中位数；
- 价格收益与成交量变化的滚动相关；
- Amihud 非流动性、零成交/停牌天数、订单容量占成交额比例；
- 极端换手、极端振幅、涨停接近度，允许模型学习过热惩罚。

**市场与行业**

- 市场 MA20/60/120 广度及 5 日变化；
- 全市场创新高/新低比例、收益分散度；
- 行业 5/20/60 日超额收益；
- 行业内上涨比例、创新高比例、VCP 候选比例；
- 个股相对行业的 5/20/60 日收益和波动；
- 市场/行业字段与个股突破质量的少量预登记交互。

市场广度和行业强度在首轮作为连续特征，不作为硬开关。

“距 252 日高点”作为候选特征有国际样本依据，但仍需在本地 A 股和当前执行规则下重新验证；相关研究在 20 个市场中有 18 个呈正收益、10 个显著。[52-week high momentum](https://www.sciencedirect.com/science/article/pii/S0261560610001099)

### 6.4 模型阶梯

1. 规则基线：当前分数；
2. 单因子和等权方向基线；
3. `Ridge/ElasticNet` 回归与 L2 Logistic；
4. 浅层 `HistGradientBoosting` 或 LightGBM；
5. 只有第 4 层稳定超过第 3 层，才保留非线性模型。

当前虚拟环境已有 `scikit-learn 1.2.2`，没有 LightGBM、XGBoost 或 CatBoost。首版无需新增重依赖即可完成线性和浅层树基线。

## 7. P0：一日跟随确认

这是最直接针对 48% 假突破率的机制实验：

```text
t 日收盘：产生 VCP 候选并冻结突破位、止损和调整因子
t+1 日收盘：仍在突破位上方，且未触发风险失效
t+1 日收盘后：生成新的 TradeIntent
t+2 日开盘：通过原风险与执行器撮合
```

必须单独报告：

- 少交易了多少信号；
- 过滤掉的假突破和赢家分别有多少；
- 延迟一天带来的价格劣化、跳空和成本；
- 成本后收益、最大回撤、平均 R 和入场日簇数。

如果确认减少亏损但同样错过大部分趋势利润，该策略应被拒绝，不能通过修改确认阈值继续搜索。

## 8. P1：Alpha158-lite 宽股票池策略

VCP 每年信号分布很不均匀，组合依赖少数突破赢家。建立第二条独立策略可以增加样本并检验“技术横截面排序在本地数据上是否有基础预测力”。

### 8.1 股票池

使用 `SelectionPanelV2.signal_eligible`，再加最低成交额、最低上市历史和容量约束。所有条件必须是当日可见值。保留板块、ST、停牌、退市和涨跌停的现有逻辑。

### 8.2 标签与组合

- 标签从 `t+1` 可执行开盘开始，首轮分别研究 5 日和 20 日市场超额收益；
- 每日产生横截面分数；
- 组合使用 top-k 和保留缓冲区，只有持仓跌出较宽排名区间才替换，避免机械周度全换；
- 新买入继续通过当前风险引擎和执行器；
- 卖出由排名退出、风险退出和最长停滞条件共同决定，分别记录原因。

这条策略必须独立通过成本后检验，不能用来给 VCP 的负结果做组合掩盖。

## 9. P2：两类后续策略

### 9.1 注意力过热与题材扩散

现有短线情绪数据层适合构造：

- 全市场涨停/跌停、炸板、连板高度及其变化；
- 题材内上涨比例、涨停比例、成交额扩张和龙头集中度；
- 个股在题材中的相对位置；
- 题材热度从扩散转为极端集中时的拥挤度。

首轮应检验 U 型、倒 U 型或分位桶，不预设正向。必须等不可变前瞻快照形成足够样本后再评估。

A 股研究显示，月内多次触及涨跌停形成的高注意力对后续横截面收益呈负向预测，并与散户净买入压力有关。这进一步说明涨停和题材热度不能固定作为正权重。[Investor attention, aggregate limit-hits, and stock returns](https://www.sciencedirect.com/science/article/pii/S1057521922002216)

### 9.2 公告后漂移

所需 PIT 数据至少包括公告实际发布时间、交易所接收时间、业绩预告/快报、报告期、原值、修正值和可比较历史。财务字段按公告可用日进入特征，而不是按报告期回填。中国市场已有注意力约束与公告后漂移研究，这为独立事件策略提供了假设基础。[中国 A 股注意力与 PEAD](https://xbbjb.cufe.edu.cn/EN/Y2021/V0/I6/27)

## 10. 验证设计

### 10.1 历史研究分层

当前历史已被多次观察，后续历史结果只能是开发集证据。建议：

- 扩展到更早的 PIT 数据后，使用 expanding walk-forward；
- 每个测试块之前只使用更早的数据训练和定标；
- 20 日标签至少设置 20 个交易日 purge/embargo，避免相邻样本共享未来收益；
- 所有截面统计以信号日为簇，不能把同日几千只股票当成独立样本；
- 特征标准化、缺失值规则和行业中性化都只能在训练窗口拟合。

### 10.2 评价门槛

新排序进入组合回测前，至少满足：

1. 样本外 Rank IC 为正，按日期聚类的置信区间不明显依赖单一年份；
2. 实际入选股票相对同日候选的 20 日收益或实现 R 有正提升；
3. 假突破率相对基线下降，且没有等比例丢失非假突破赢家；
4. 因子覆盖率、缺失拒绝数和共同样本结果完整披露；
5. 线性模型、浅层树和规则基线使用同一股票池、日期和标签；
6. 成本后组合优于匹配 placebo，且换手、容量、行业集中和尾部风险可接受。

### 10.3 防止过拟合

- 注册每一次试验，不只注册最终保留版本；
- 参数只在训练/验证块选择，测试块一次性揭盲；
- 多个因子和标签使用 FDR；
- 模型随机性至少运行 10 个种子，报告均值和离散度；
- 策略层报告 Deflated Sharpe Ratio 和 Probability of Backtest Overfitting；
- 真正准入仍需从下一未观察交易日起至少 6 个月、30 个独立入场日簇的冻结模拟盘。

## 11. 对现有系统的影响

### 11.1 可以直接复用

- `SelectionPanelV2` 的 PIT 股票池、生命周期和字段可用性；
- `TradeIntent`、风险预审批、订单、成交、持仓和退出账本；
- 原始/复权价格映射、涨跌停、T+1、停牌和退市处理；
- placebo、压力测试、可视化和模拟盘通知；
- 当前市场广度、行业映射和短线情绪不可变快照。

### 11.2 需要新增

```text
FeatureSnapshotBuilder
  -> PIT features keyed by (signal_asof, symbol, feature_version)
LabelBuilder
  -> executable forward labels, research-only
WalkForwardSplitter
  -> expanding train/validation/test + purge/embargo
CrossSectionalPreprocessor
  -> train-only winsorize/normalize/neutralize
SelectionModel
  -> fit / predict / immutable model manifest
TrialRegistry
  -> every attempted hypothesis and parameter budget
ScoreToIntentAdapter
  -> model score -> existing TradeIntent
```

模型层不能读取执行器的未来成交结果；标签文件不得被模拟盘进程加载。特征快照、训练数据、模型和预测均保存哈希。

## 12. 建议实施顺序

1. 冻结本研究方案和试验预算；
2. 建立特征、标签、时间切分和 trial registry 基础设施；
3. 实现 VCP 25 至 40 个候选质量特征，运行单因子覆盖率、冗余和样本外 IC；
4. 实现线性两阶段 `vcp_quality_rank_v1`；
5. 独立实现 `vcp_followthrough_v1`；
6. 两者分别通过后，再运行统一风险执行回测；
7. 建立 `alpha158_lite_v1` 的宽股票池线性基线和 top-k buffer 组合；
8. 数据期扩展后才试浅层 LightGBM；
9. 等短线情绪前瞻样本后研究注意力过热；
10. 补齐 PIT 公告和财务数据后研究 PEAD；
11. 所有历史研究完成后，冻结版本进入长期模拟盘。

## 13. 最终判断

最优的下一步不是再加一个硬过滤条件，而是把当前 VCP 从“人工等权打分”改造成“预测假突破风险与条件收益的可验证排序器”。这能直接对应当前 79,149 元假突破亏损来源，也能最大限度复用已经修正过的 PIT、风控和执行框架。

同时应建立 `alpha158_lite_v1` 作为独立宽股票池基线。它的作用是判断本地五年数据是否足以支持稳定的横截面排序，并为未来的行业、注意力和基本面因子提供统一承载层。若简单正则化模型都无法在滚动样本外获得正 IC，则不应升级到深度学习或强化学习。
