# Alpha158 机器学习因子优化研究 v1

| 项目 | 冻结定义 |
|---|---|
| 版本 | `alpha158_ml_factor_optimization_v1` |
| 文档日期 | 2026-10-07 |
| 目标 | 建立可复用、只读正式账户的机器学习研究能力，并在不改变因子、标签、候选池、组合和执行规则的前提下验证有限模型候选 |
| 证据等级 | 已观察历史上的模型族筛选，只能决定是否启动前瞻 shadow |
| 正式策略 | 继续使用冻结的 26 因子 Ridge 和现有 `event_exit_only` 交易规则 |
| 自动准入 | 禁止；历史通过、前瞻通过后均需要人工复核 |

## 1. 背景与研究问题

当前 Alpha158-lite 模型使用 26 个日线价格、趋势、波动、流动性、K 线和行业相对因子，
对每个信号日做横截面居中百分位排名，再使用带训练期中位数填充的
`Ridge(alpha=100)` 预测未来 20 日可执行收益排名。每个信号日的训练样本总权重相同。

已有浅层 `HistGradientBoostingRegressor` 历史实验提高了平均 Rank IC，但 Top10 质量和账户
收益下降，因此不能依据因子层指标替换模型。该结果只拒绝当时固定的点式回归树规格，
不等同于否定全部非线性模型。

本研究第一阶段只回答两个问题：

1. 在完全相同的 26 个输入因子下，ElasticNet 的稀疏正则能否改善历史滚动样本外表现、
   跨折稳定性和模型简洁度？
2. 在保持原始收益口径不变的条件下，LightGBM LambdaRank 对每日头部排序的建模，能否
   改善相同组合与执行规则下的成本后账户表现？

“ElasticNet 系数为零”只表示某因子在特定训练窗口和正则强度下没有被模型保留，不得解释为
该因子长期无经济价值。“LambdaRank 更贴近头部排序”也只是待验证假设，不预设它比 Ridge
更可能盈利。

### 1.1 当前策略身份

当前两条表现最好的评分规则不是同等级的正式策略：

| 评分规则 | 当前身份 | 机器学习用途 |
|---|---|---|
| 26 因子 Ridge | 正式冻结基线 | 模型替换实验的冠军对照 |
| `all_mean_rank` | 历史候选，尚未替换正式策略 | 独立的分数组合实验对照 |

`all_mean_rank` 与 Ridge 共用大量数据、股票池和执行规则，不能当成相互独立的两条 Alpha 袖套。
v1 不训练两者之间的动态资金分配器，也不把两条历史曲线拼接成一个训练目标。

### 1.2 独立机器学习研究能力

机器学习研究层与正式模拟盘共享冻结数据、组合风险和执行基础设施，但使用独立注册、输出目录、
模型产物和账户。总体数据流为：

```text
冻结策略注册表 + PIT 数据快照
-> 策略 ML 适配器
-> 标准 ML 数据集
-> Purged Walk-Forward
-> 模型插件
-> 严格 OOS 分数仓库
-> 现有组合、风险与执行器
-> 账户和风险报告
-> 历史筛选决定
-> 独立前瞻 shadow（仅通过者）
```

研究层只允许输出每日证券分数：

```text
strategy_id
signal_asof
symbol
score
model_id
fold_id
source_manifest_id
feature_view_id
label_contract_id
```

它不能直接修改持仓、止损、风险预算、正式账户状态或企业微信交易通知。

### 1.3 通用组件合同

建议建立以下通用组件，但 v1 只实现本规格明确的模型和策略适配器：

```text
MLStrategyAdapter       # 将冻结策略转换为特征、标签、预测股票池和组合协议
MLResearchDataset       # 保存 as-of 特征、标签成熟状态、query 和权重
MLModelPlugin           # fit / predict / export_artifact
MLOOSPredictionStore    # 只接受时间顺序 OOS 分数
MLPortfolioEvaluator    # 复用现有组合、风险和执行器
MLExperimentRegistry    # 预登记、配置哈希、实验预算和状态机
MLDecisionEngine        # 按冻结门槛机械生成决定
```

标准数据集至少包含：

```text
strategy_id, signal_asof, symbol
feature_eligible_asof
label, label_end, label_available_at, label_mature, label_valid
query_id, sample_weight
feature_view_id, label_contract_id
```

策略适配器只能声明已有、可审计的数据视图，不能在模型运行时临时增加字段。

### 1.4 支持的实验类型

v1 平台支持两类相互独立的实验：

1. `score_replacement`：保持原始因子与标签不变，用新模型替换评分器。26 因子 Ridge、
   ElasticNet 和 LambdaRank 属于此类。
2. `score_combiner`：只读取已经严格 OOS 生成的多个子模型分数，学习受约束的组合分数。
   `all_mean_rank` 后续实验属于此类。

元标签、市场门禁、仓位模型、退出模型和策略级资金分配均不是 v1 实验类型。

## 2. 非目标

v1 明确不包含：

- 不增加、删除或事后筛选 26 个输入因子；
- 不把标签改成行业中性收益、个股绝对收益或实际事件退出收益；
- 不修改股票池、流动性门槛、ST 策略和历史 PIT 规则；
- 不修改每 5 个交易日评审、连续两次入围、目标持仓数和事件退出规则；
- 不修改仓位、开放风险、行业风险、容量和压力损失约束；
- 不研究元标签、市场门禁、动态仓位、M1/M2模型融合或自动无界因子权重搜索；
- 不训练 Ridge 与 `all_mean_rank` 之间的动态资金分配；
- 不研究 MLP、TabNet、Transformer、图模型、强化学习或 LLM 直接评分；
- 不覆盖或延迟 `all_mean_rank` 已有研究决定；若其前瞻 shadow 已注册，不修改其 genesis、
  账户或评估时钟；
- 不接入正式模拟盘、企业微信交易通知或实盘账户。

行业中性标签、M1/M2模型融合和元标签均属于独立的未来研究，不能在看到本实验结果后追加为
同一个模型替换实验的候选臂。`all_mean_rank` 受约束分数组合使用独立实验族，不构成例外追加。

## 3. 证据边界

统一可比的滚动测试区间为 2023-07-27 至 2026-09-30，共 772 个交易日。虽然每个测试预测
相对于对应训练折是时间顺序样本外预测，但这些日期已经被既往研究观察，不能称为新的独立
留出集。

v1 的最大历史结论为：

```text
PASS_HISTORICAL_SCREEN
-> 允许注册一个规则冻结的前瞻 shadow 候选
```

禁止从历史结果直接产生：

```text
ADMIT_TO_FORMAL_PAPER
ADMIT_TO_LIVE
```

本实验与以前的因子族、浅层非线性、估值、龙虎榜和等权因子族实验共同构成已进行过的研究
尝试。报告必须列出这些既往尝试，不能把 v1 当作只比较两个全新假设的独立显著性检验。

## 4. 不变量

同一 `score_replacement` 实验内的三条实验臂必须共用以下输入与执行条件：

- 同一个 `SelectionPanelV2` 数据快照和数据清单；
- 相同 26 个 `alpha158_lite_features_v1` 字段及字段顺序；
- 相同信号日横截面居中百分位排名；
- 相同训练期中位数缺失值填充，禁止用验证或测试数据拟合填充值；
- 相同训练日期、验证日期、测试日期和训练日期 5 日抽样步长；
- 相同训练日期权重合同；Ridge、ElasticNet 使用逐行日期等权，LambdaRank 训练使用本规格冻结的
  query 内常数权重。LambdaRank 验证指标不继承训练逐行权重，按日期 query 等权聚合；不同损失
  函数的实际梯度贡献不宣称完全相等；
- 相同标签可执行性过滤和未来收益定义；
- 相同股票评分深度、排序稳定规则、风险审批和撮合执行器；
- 相同初始资金、公司行为、停牌、涨跌停、T+1、费用和滑点规则；
- 相同随机组合之外的全部报告口径。

实现新实验前，必须先用新入口重放 Ridge 对照。其 OOS 预测、每日排名、订单、成交、账户曲线
和汇总指标必须与登记基线在冻结容差内一致。基线不能复现时，实验失败关闭，不得继续比较
ElasticNet 或 LambdaRank。

冻结容差为：样本键与标签版本哈希完全一致；每日股票排名、订单和成交业务字段完全一致；
Ridge 分数最大绝对误差不超过 `1e-12`；每日现金和净值差不超过人民币 `0.01` 元。若既有
产物精度不足以验证某项，必须先明确生成新的共同基线，不能降低容差直至通过。

## 5. 标签合同

### 5.1 统一经济标签

三种模型统一使用现有 `alpha158_lite_excess20_v1` 经济标签：

```text
信号日 t 收盘后产生样本
-> 从 t+1 的首个可执行原始开盘价开始
-> 持有到 t+20 收盘
-> 扣除同期沪深 300 收益
-> 在同一信号日的可执行样本内转为横截面收益排名
```

训练标签沿用现有可执行性规则：25bp 买入侧滑点只参与“次日订单是否仍低于涨停价和冻结最高
买价”的资格判断；连续收益目标本身仍使用调整后次日开盘到 `t+20` 收盘的收益，不再额外扣除
滑点或卖出费用。减去同一沪深300区间收益不会改变同日股票的横截面顺序，但必须保留该字段
定义和版本哈希，以保证与现有模型完全一致。

### 5.2 LambdaRank 必要等级编码

LightGBM LambdaRank 只允许对上述同一个未来收益排序做单调的整数等级编码。对某个具有成熟
有效标签的 query，设：

```text
n   = query 内成熟有效标签数量
r_i = 证券 i 按未来收益降序排列的平均名次，允许为小数
m   = n / 2
```

等级必须按以下顺序执行，第一条命中的分支即为唯一结果：

```python
if r_i > m:
    relevance = 0
elif r_i <= 10:
    relevance = 5
elif r_i <= 20:
    relevance = 4
elif r_i <= 50:
    relevance = 3
elif r_i <= 100:
    relevance = 2
else:
    relevance = 1
```

该顺序使“后 50%”优先于固定 Top100，避免小 query 中的区间重叠；`10.5`、`20.5` 等平均
名次也能唯一落入下一档。跨越边界的并列收益组使用同一个平均名次并获得同一等级。每个成熟
有效样本必须恰好命中一个分支。

若一个 query 映射后少于两个不同 relevance，它没有有效排序 pair：该 query 不参与
LambdaRank 训练、验证指标或早停，并记录为 `DEGENERATE_QUERY_NO_PAIR`。这不影响该信号日
作为交易预测日期，也不允许删除当日 as-of 合格股票。

编码只能由成熟有效标签生成；预测时模型看不到等级或未来收益。

`label_gain` 固定为线性增益 `[0, 1, 2, 3, 4, 5]`，禁止使用结果揭示后再切换默认指数增益。
训练截断固定为 `lambdarank_truncation_level=20`；同时报告 `NDCG@10`、`NDCG@20` 和
`NDCG@50`，但这些只属于模型诊断指标。

行业中性化会改变股票之间的标签次序，禁止混入 v1。若以后研究行业中性标签，必须保持模型、
参数和执行规则不变并登记为独立消融。

## 6. 时间切分与成熟标签

沿用现有 `PurgedWalkForward`：

- expanding train；
- 至少 252 个训练信号日；
- 63 个验证信号日；
- 63 个测试信号日；
- 标签期限 20 个交易日；
- 额外 embargo 为 0；
- 训练日期每 5 日抽取一次；
- 测试期逐日预测。

`embargo_sessions=0` 只表示标签成熟后的额外空白期为零。每折必须满足：

```text
train_label_end < validation_start
validation_label_end < test_start
```

每折 manifest 必须保存训练、验证、测试区间，训练与验证标签最后成熟日，实际清除交易日数，
输入数据哈希和模型配置哈希。任何边界缺失、训练或验证使用未成熟标签、测试日期重叠或未来
数据影响历史特征的情况都必须失败关闭。

`trained_until` 不能单独作为无泄漏证明。每折模型来源 manifest 至少保存：

```text
train_signal_max
train_label_end_max
train_label_available_at_max
validation_signal_max
validation_label_end_max
validation_label_available_at_max
model_selected_at
model_available_at
prediction_block_start
parent_manifest_ids
parent_manifest_hashes
```

`label_available_at` 是标签所需最后一笔行情实际可用于研究进程的时间，而不只是标签起始信号日。
每个测试块必须满足：

```text
train_label_available_at_max < validation_start_asof
validation_label_available_at_max < prediction_block_start_asof
model_available_at <= 每条预测的 signal_asof
```

模型选择、网格选择和早停均属于验证信息使用，必须包含在 `validation_*` 与
`model_selected_at` 证明中。OOS 仓库必须从预测记录的 `source_manifest_id` 递归校验完整父
manifest 哈希链；缺父节点、哈希不一致或任一上游标签成熟时间越界时，整批预测失败关闭。

### 6.1 训练、诊断和交易预测必须分离

数据集必须保存并分别使用以下掩码：

| 掩码 | 信息边界 | 用途 |
|---|---|---|
| `feature_eligible_asof` | 只读取信号日及以前 | 当日模型评分和交易候选 |
| `label_mature` | 完整 20 日结果已经发生 | 允许进入监督学习或诊断 |
| `label_valid` | 标签成熟且现有标签执行资格有效 | 训练、验证和标签诊断 |
| `diagnostic_valid` | OOS 预测与成熟有效标签同时存在 | IC、NDCG、TopK 诊断 |

训练和验证只使用 `feature_eligible_asof AND label_mature AND label_valid`。交易预测必须保留信号
日当时 `feature_eligible_asof` 的全部股票，不得按次日停牌、涨停、跳空、未来收益或
`target_rank` 是否为空过滤。历史执行器在次日实际处理拒单和延期。

测试区间尾部 20 日标签尚未成熟属于正常状态：这些日期继续评分、产生候选并进入账户回放，
但不进入 IC、NDCG 和 TopK 标签诊断。未成熟测试标签不能导致实验失败，也不能缩短账户评价
区间。

## 7. 实验臂

### 7.1 M0：冻结 Ridge 对照

- 模型：`Ridge(alpha=100, fit_intercept=True)`；
- 输入：训练期中位数填充后的 26 个居中百分位因子；
- 目标：连续的 `target_rank`；
- 权重：每个信号日总权重相同；
- 参数选择：无；
- 作用：验证新实验入口能够复现当前冻结基线。

### 7.2 M1：ElasticNet

ElasticNet 与 Ridge 使用完全相同的连续 `target_rank`、输入矩阵、样本权重和折。固定实现参数：

```text
fit_intercept = true
positive = false
selection = cyclic
max_iter = 20000
tol = 1e-6
precompute = true
```

只允许以下小网格：

```text
alpha    = [0.00001, 0.0001, 0.001, 0.01]
l1_ratio = [0.10, 0.50, 0.90]
```

每折只使用该折验证集上的日期等权目标均方误差选择参数。若误差在 `1e-12` 内并列，依次选择
更大的 `alpha`、更大的 `l1_ratio`，以得到更保守的模型。测试集不能参与参数选择。

报告每折非零系数数目、符号、绝对权重排名和相邻折稳定性，但不得据此回删因子或重跑 v1。
若网格全部不收敛，该折候选失败；禁止临时扩大迭代次数或网格。

### 7.3 M2：LightGBM LambdaRank

每个 `signal_asof` 是一个 query group，同日所有可执行训练样本必须连续进入同一个 group。
禁止把股票随机拆分到训练、验证和测试中。v1 只使用一个保守配置，不做树深、叶数、学习率、
特征子采样或标签增益搜索：

```text
objective = lambdarank
metric = ndcg
ndcg_eval_at = [10, 20, 50]
lambdarank_truncation_level = 20
lambdarank_norm = true
label_gain = [0, 1, 2, 3, 4, 5]
learning_rate = 0.03
num_leaves = 7
max_depth = 3
min_data_in_leaf = 500
lambda_l1 = 1.0
lambda_l2 = 10.0
feature_fraction = 1.0
bagging_fraction = 1.0
bagging_freq = 0
max_bin = 63
num_iterations = 300
early_stopping_rounds = 30
first_metric_only = true
deterministic = true
force_col_wise = true
seed = 20261007
```

最佳迭代数只根据该折验证集 `NDCG@10` 选择。实现必须显式使用
`early_stopping(stopping_rounds=30, first_metric_only=True)` 或经过测试的等价接口，并保证
评估输出中的第一个指标是 `NDCG@10`。`NDCG@20` 或 `NDCG@50` 停滞不得在
`NDCG@10` 仍改善时触发早停。

若安装的 LightGBM 版本不支持上述参数、确定性模式、早停合同或标签合同，实验失败关闭；
禁止静默删除参数、换用其他目标或回退到普通回归树。

### 7.4 LambdaRank 训练权重与验证指标权重

训练与验证 Dataset 必须分开处理。设某折训练行数为 `N_train`、有效 query 数为 `Q_train`、
query `q` 的行数为 `n_q`。训练 Dataset 传入逐行权重：

```text
w_train(q, i) = N_train / (Q_train * n_q)
```

同一训练 query 内所有行权重相同，每个 query 的输入权重总和必须在 `1e-12` 容差内相等。
`group` 边界、排序后的行和训练 `weight` 数组必须一一对应。

验证 Dataset 不传入逐行权重。LightGBM 原生 NDCG 在没有 query weight 时对 query 做等权
平均，这与“每个交易日对早停贡献相同”的合同一致。禁止把训练权重
`N / (Q * n_q)` 传给验证 Dataset：LightGBM 会使用 query 内逐行权重的平均值作为 query
权重，从而使验证日期权重与 `1 / n_q` 成正比并偏向股票较少的日期。

实现必须将 LightGBM 验证 NDCG 与手工计算的逐日 NDCG 算术平均值对账；不同 query 大小下
误差不得超过 `1e-12`。若所冻结 LightGBM 版本无法满足该合同，改用经过同等测试的自定义日期
等权 NDCG，而不是恢复验证逐行权重。

`lambdarank_norm=true` 保留，但它不替代上述输入权重，也不保证不同 query 对实际目标梯度的
贡献严格相等。pair 数量、标签结构和 NDCG 增益造成的实际贡献差异属于模型机制，按 query
规模分桶报告，不设置“贡献必须相等”的虚假硬门槛，也不通过复制或删除样本修正。

## 8. 组合与执行合同

三条实验臂只提供每日股票分数，不能直接生成不同的交易规则。分数统一交给现有组合链：

- 目标持仓 10 只；
- 每 5 个交易日评审；
- 连续两次评审进入前 50 才允许买入；
- 组合版本固定为 `alpha158_price_only_no_rank_exit_v1`；
- 底层低换手规则固定为 `alpha158_lite_low_turnover_v3`；
- `review_overlay` 固定为 `suppress_rank_exits`，即不因持续排名下降退出；
- 初始止损、移动止损和停滞退出逐日执行；
- 同日卖单先于买单；
- 复用现有组合风险审批、容量和压力损失；
- 复用 `pit_corrected` 原始价格执行器。

实验配置必须显式保存实际采用的组合策略版本和 `review_overlay`。禁止依赖调用方默认值，避免
把 `event_exit_only` 与早期排名退出版本混用。

成本情景固定为：

| 情景 | 滑点 | 其他费用 |
|---|---:|---|
| 主情景 | 买入和卖出各 25bp | 佣金、最低佣金、过户费、卖出印花税另计 |
| 压力一 | 买入和卖出各 40bp | 同上 |
| 压力二 | 买入和卖出各 60bp | 同上 |

三档成本都必须从相同初始状态完整重放。由于成本可能改变现金、风险额度和后续成交，禁止在
25bp 固定成交结果上事后线性扣减得到 40/60bp 结果。

## 9. 指标与统计

### 9.1 主要经济指标

每个实验由注册中的 `candidate_arm_id` 和 `baseline_arm_id` 唯一确定对照关系。主要指标为主
成本情景下，候选相对其注册对照的成本后账户年化收益增量。累计收益增量同时报告，但不单独
作为准入门槛。

`score_replacement` 的 `baseline_arm_id` 固定指向 26 因子 Ridge；`score_combiner` 的 A1
实验固定指向七族等权 A0。报告、Bootstrap、决策器和前瞻 shadow 必须读取并校验相同的
`baseline_arm_id`，禁止在下游重新硬编码 Ridge。

对每个候选都报告：

- 成本后累计收益、年化收益和相对注册对照增量；
- 最大回撤、Calmar、日度 ES95；
- 三次连续跌停清算收益和最大回撤；
- 平均股票仓位、平均资金暴露、开放风险和行业集中度；
- 买入次数、买入日期簇、换手、总摩擦成本和容量拒单；
- 年度和冻结市场状态切片；
- 收益对最佳 5 笔和 10 笔交易的集中度。

### 9.2 诊断指标

以下指标只解释模型行为，不能覆盖账户门槛：

- 全截面日均 Rank IC 和年度 Rank IC；
- Top10、Top20 未来 20 日超额收益提升；
- LambdaRank 的 NDCG@10、20、50；
- 与注册对照的 Top10、Top20、Top50 选择重合率；
- 分数分布、行业暴露、换手和候选排名迁移；
- ElasticNet 系数稳定性；
- LambdaRank split/gain importance 的跨折稳定性。

全截面 Rank IC 可以下降，它在 v1 中不构成独立否决条件；Rank IC 提高也不能弥补账户收益
或风险失败。

### 9.3 不确定性

将共同测试日期上的候选与注册对照日净收益保存为两列对齐序列，使用同一组移动区块索引同步
抽样。每条重采样路径必须：

1. 分别从初始净值 1 重建候选和注册对照净值；
2. 分别计算累计收益、年化收益、最大回撤和 ES95；
3. 最后计算候选指标减注册对照指标；
4. 对指标差值分布计算区间。

禁止先构造“候选日收益减 Ridge 日收益”再从差值序列计算累计收益、回撤或 ES。日收益差序列
只允许用于平均日收益差等线性诊断。

移动区块 Bootstrap 配置为：

- 主区块长度 20 个交易日；
- 敏感性区块长度 40、60 个交易日；
- 每种长度 5,000 次；
- 固定随机种子 `20261007`；
- 报告年化收益增量、累计收益增量、最大回撤增量和 ES 增量区间。

区块重采样只描述已实现 OOS 交易路径的不确定性，不声称重新生成了具有不同订单路径的完整
市场历史。不得根据区间结果选择最有利的区块长度。

M1、M2 是同一个研究族的两个预登记候选。v1 不计算或报告 Holm 调整 p 值，也不声称控制了
完整历史研究过程的多重比较错误；报告必须列示本研究族及此前相关模型和因子实验数量。由于
历史已观察，统计区间只作筛选证据，不解释为独立确认性显著性。

### 9.4 收益集中度

最佳交易集中度使用候选账户已关闭逻辑交易的成本后净利润，定义为：

```text
top5_positive_profit_share
= 最大 5 笔正净利润之和 / 全部正净利润之和
```

没有正利润交易时该指标为缺失并导致集中度门槛失败。该指标是静态归因，不删除交易、不释放
资金、不重新运行后续订单，也不解释相对注册对照增量的完整来源。

同时报告按入场时 PIT 行业聚合的正净利润占比、收益增量按行业的静态归因以及未知行业比例。
由于当前没有冻结且经过验证的单行业阈值，单行业集中度在 v1 中只作诊断，不参与机械决定。

## 10. 历史筛选门槛

候选必须同时满足以下条件才可记为 `PASS_HISTORICAL_SCREEN`：

1. 主成本情景年化收益相对注册对照增量至少 `+0.50` 个百分点；
2. 25/40/60bp 三档成本的累计收益均高于对应注册对照；
3. 三档成本的最大回撤、日度 ES95 和三跌停清算收益均不劣于对应注册对照；
4. 主成本情景 Calmar 高于注册对照；
5. 三档成本的平均股票仓位绝对差均不超过 1 个百分点；
6. 主成本情景配对年化收益增量的 20 日区块 95% 区间下界大于零；
7. 至少三个年度切片的账户收益增量为正；
8. `top5_positive_profit_share <= 0.50`；
9. 所有时间隔离、基线复现、确定性和执行一致性检查通过。

20 日区间下界是唯一参与机械决定的 Bootstrap 门槛。40/60 日使用相同双曲线同步重采样方法
报告完整区间、区间宽度和分布中位数，只作相关长度敏感性诊断，不参与
`PASS / INCONCLUSIVE / REJECT` 状态映射。原始账户点估计与区块长度无关，禁止将其包装成
40/60 日独立门槛。若某项决定性指标因样本不足无法计算，则记为
`INCONCLUSIVE_HISTORICAL_SCREEN`，不按零或通过处理。

Rank IC、NDCG、Top10收益或稀疏度没有独立的一票通过权。任何候选失败后不得根据其结果调整
标签分档、参数网格、树深、成本或组合规则并继续沿用 v1 名称。

## 11. 决策状态

每条候选臂只能进入以下状态之一：

```text
REJECT_BASELINE_NOT_REPRODUCED
REJECT_DATA_OR_LEAKAGE_AUDIT
REJECT_HISTORICAL_SCREEN
INCONCLUSIVE_INCOMPLETE_COVERAGE
INCONCLUSIVE_HISTORICAL_SCREEN
PASS_HISTORICAL_SCREEN
```

每个实验在训练前必须登记预期预测键集合及其哈希：

```text
expected_prediction_key = (signal_asof, symbol, fold_id, candidate_arm_id)
```

实际 OOS 预测键必须与预期集合完全相等。任一必需折训练失败、缺少预测、产生额外预测或静默
回退到对照模型时，实验进入 `INCONCLUSIVE_INCOMPLETE_COVERAGE`，不得计算部分区间的历史
通过结论，不得删除失败折或缩短评价区间。预测覆盖必须为 100%；测试尾部未成熟标签只降低
诊断覆盖率，不降低预测覆盖率。

M1 和 M2 分别判定，不因另一模型表现更好而自动拒绝。二者彼此融合可能具有互补性，但不属于
本次26因子模型替换实验；只有在新的预登记文档中才能独立研究，且不得根据本次测试区间搜索
融合权重。

### 11.1 `all_mean_rank` 分数组合实验

`all_mean_rank` 使用独立的 `experiment_family_id` 和注册配置，不与 M1/M2 合并判定。其对照是
七个因子族当日 OOS 排名的固定等权平均，不是 26 因子 Ridge。

该适配器只允许读取以下七列已经严格 OOS 生成的分数，并在每个交易日再次转为横截面排名：

```text
family_regression_trend_rank
family_price_position_rank
family_volume_structure_rank
family_price_volume_persistence_rank
family_kbar_shape_rank
family_vwap_price_rank
family_residual_overheat_rank
```

七个家族分数必须携带各自 `source_manifest_id`、折号和来源哈希；OOS 仓库按第 6 节递归验证
训练标签、验证选择/早停和模型可用时间。任一父 manifest 缺失、哈希失配或使用同日/未来标签
拟合时，整批预测失败关闭。组合模型在新的外层 walk-forward 中只能使用更早日期的家族 OOS
分数训练。

首个组合实验只比较：

| 实验臂 | 定义 |
|---|---|
| A0 | 七族当日排名等权平均，权重固定为 `1/7` |
| A1 | 非负、权重和为 1、向 `1/7` 收缩的线性组合 |

A1 解以下冻结问题：

```text
score    = sum(w_i * family_rank_i)
target   = alpha158_lite_excess20_v1 的连续 target_rank
minimize = 各日期内样本均方误差的日期等权平均
           + lambda * sum((w_i - 1/7)^2)
subject to w_i >= 0
           sum(w_i) = 1
```

不使用截距。`lambda` 网格固定为 `[0.01, 0.10, 1.00, 10.00]`，只使用每折验证集的日期等权
目标均方误差选择；在 `1e-12` 内并列时选择更大的 `lambda`。优化器、收敛容差和最大迭代数
固定为 `SLSQP`、`1e-12` 和 `2000`；任一折不收敛即失败关闭。禁止负权重、杠杆权重、按完整历史一次拟合、根据
测试结果删除家族或扩大搜索网格。A1 与 A0 使用相同组合、风险、执行、成本和本规格的账户
评估方法。

LambdaRank 七族组合、26 个原始因子与七族分数混合、Ridge 与 `all_mean_rank` 的动态切换均不
进入首个组合实验。它们只有在另行登记新的实验预算后才能研究。

该实验等待 2015--2026 行情数据完成合并、审计并冻结快照后正式运行；平台接口和 Mock 自测
不需要等待数据补采。2012--2014 财务预热数据和非严格 PIT 财务敏感性结果不得进入该技术
分数组合模型。

### 11.2 长历史七族 OOS 生产与共同评价区间

长历史行情快照不会自动产生七族 OOS 分数。A0/A1 正式实验前必须完成独立交付：

1. 冻结七个家族的特征、标签、滚动折、模型和依赖配置；
2. 生成 2015--2026 各家族逐折 OOS 预测和不可变来源 manifest；
3. 递归审计训练与验证标签成熟时间、模型可用时间、股票键和来源哈希；
4. 按预登记算法确定共同可评价区间：七族首个完整 OOS 日期与执行数据首个完整日期中的较晚者，
   不得按收益表现挑选起点；
5. 冻结共同预期预测键集合，区间内任一家族缺键即失败关闭，不允许运行后静默取交集。

A0 与 A1 必须从共同区间同一交易日、相同初始资金和空仓状态开始，使用完全相同的股票池、
组合、风险、执行和成本规则。不得比较各自不同可用区间的全历史结果。

## 12. 前瞻 shadow 边界

历史通过只允许创建新的双账户前瞻 shadow：注册中的 `baseline_arm_id` 对照和一个通过候选，
空仓、等资金、等数据、等执行起步。模型、代码、依赖、来源 manifest、训练和验证标签最后
成熟时间、数据截止日和配置哈希写入 genesis；正式账户和 `all_mean_rank` 已有研究记录及任何
已经注册的 shadow 不受影响。

前瞻协议复用 `alpha158_forward_shadow_v1` 的以下原则：

- 不补做缺失交易日并冒充前瞻记录；
- 不根据收益提前停止、延长或改参；
- 第 6、9、12、18 个月按预登记节点检查；
- 两账户均达到至少 30 个买入日期簇后才做正式评估；
- 历史收益不得与前瞻账户收益拼接；
- 通过仍只获得人工复核资格，不自动接入正式模拟盘或实盘。

若 M1、M2 同时历史通过，也不能同时启动多个新 shadow 后择优。必须在不再次查看历史收益的
情况下，依据预登记优先级选择一个候选；v1 优先选择模型自由度更低的 ElasticNet。另一个候选
保留为历史研究结果，除非另行登记独立实验容量。

`all_mean_rank` 组合实验属于另一个实验族；A1 通过不与 M1/M2 做事后历史冠军评选。每个实验族
最多登记一个候选 shadow。是否同时运行两个实验族的 shadow 由独立的运行容量和人工审查决定，
不能根据已观察历史收益择优启动。

## 13. 产物和可审计性

建议冻结配置：

```text
configs/selection/alpha158_ml_factor_optimization_v1.json
configs/selection/alpha158_all_mean_ml_combiner_v1.json
configs/ml/model_registry_v1.json
```

建议研究入口：

```text
scripts/run_alpha158_ml_score_replacement_v1.py
scripts/run_alpha158_all_mean_ml_combiner_v1.py
```

每次正式运行写入不可覆盖的新目录，至少包含：

```text
registration.json
data_manifest.json
environment_manifest.json
fold_manifests.json
model_selection.json
oos_predictions.csv.gz
factor_metrics.csv
portfolio_metrics.csv
yearly_metrics.csv
account_curves/
orders/
fills/
bootstrap_results.json
report.json
```

`registration.json` 必须在运行前生成并包含规范化配置 SHA256、Git 提交、工作区是否干净、数据
清单哈希、依赖版本、实验族 ID、`candidate_arm_id`、`baseline_arm_id`、预期预测键集合哈希、
既往相关实验清单和输出目录。结果文件不能回写注册文件。

模型产物必须保存每折的训练截止日、验证选择、参数、迭代数、特征顺序、填充值、依赖版本和
文件哈希。LightGBM 不可用时只允许明确失败，禁止自动安装未知版本或切换到 XGBoost、
CatBoost、随机森林及普通梯度提升模型。

## 14. 测试与失败关闭

实现至少需要覆盖：

1. Ridge 新入口与冻结基线预测和账户结果一致；
2. 训练、验证、测试标签结束日严格隔离；
3. 测试区间尾部未成熟标签不缩短预测或账户区间，也不导致实验失败；
4. 修改信号日之后的未来标签和成交条件，不改变该信号日候选、特征和预测分数；
5. 训练、验证和标签诊断只读取成熟有效标签；
6. 三模型使用完全相同的 as-of 预测股票行和 26 因子顺序；
7. Ridge、ElasticNet 每日输入权重和与 LambdaRank 训练 query 输入权重和分别满足日期等权合同；
   LambdaRank 验证 NDCG 与手工逐日等权聚合一致且不偏向小 query；
8. LambdaRank query 按日期连续、无跨日混组，每个成熟有效样本唯一获得一个等级；
9. 覆盖半整数平均名次、小 query、跨边界并列和全体收益并列的等级测试；
10. 退化 query 不进入 LambdaRank 训练和早停，但当日股票仍获得交易预测；
11. ElasticNet 只使用验证集选择网格，测试集变化不改变所选参数；
12. LightGBM 早停只使用验证集 `NDCG@10`；其他 NDCG 停滞不能提前结束训练；
13. 同一组区块索引分别重建两账户净值后再计算非线性指标差；差收益曲线不得替代该流程；
14. 20 日主区间和集中度边界能机械映射到唯一决定状态；40/60 日结果仅进入诊断；
15. 相同输入重复运行产生相同预测、订单、成交和报告哈希；
16. 25/40/60bp 均按单边滑点完整重放并另计费用；
17. 缺模型依赖、缺交易日、数据哈希变化、训练或验证标签未成熟、模型不收敛时失败关闭；
18. 正式策略、企业微信模拟盘和既有 shadow 状态在研究前后完全不变；
19. 报告状态由冻结门槛机械生成，不能由脚本参数人工覆盖；
20. 训练日期合法但训练标签、验证标签、早停或调参信息越界时，来源 manifest 递归审计拒绝；
21. 预期与实际预测键必须完全相等；任一必需折失败不得删折、缩短区间或回退 Ridge；
22. A1 的注册、账户、Bootstrap、报告、决定和 shadow 全程使用 A0 对照，任何 Ridge 对照调用
    都必须失败；
23. 长历史七族来源在共同区间内完整，A0/A1 从同日、等资金、空仓状态开始。

## 15. 实施顺序

实现时按以下顺序推进，每一阶段完成独立自测后才能进入下一阶段：

1. 建立实验注册、策略适配器、标准数据集和 OOS 分数仓库；
2. 冻结配置、输出目录并复现 26 因子 Ridge 基线；
3. 抽取统一训练矩阵、标签掩码、权重和 walk-forward manifest；
4. 实现并验证 ElasticNet 单臂；
5. 实现并验证 LambdaRank 等级、query、日期权重和确定性训练；
6. 接入相同组合及三档成本完整重放；
7. 生成因子、账户、风险、同步双曲线区块区间和集中度报告；
8. 由冻结门槛生成 26 因子模型实验的历史决定；
9. 在长历史快照完成后，冻结并生产七族长历史严格 OOS 分数及递归来源 manifest；
10. 冻结 A0/A1 共同区间和预测键，从同日、等资金、空仓状态运行受约束组合实验；
11. 若且仅若某一独立候选通过，另行生成前瞻 shadow 注册材料并等待人工确认。

本规格批准的是受控历史研究，不批准策略切换、正式模拟盘变更、实盘交易或基于结果继续调参。
