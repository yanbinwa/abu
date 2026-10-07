# Alpha158 机器学习研究能力 v1 开发计划

| 项目 | 内容 |
|---|---|
| 状态 | Planned |
| 计划版本 | `alpha158_ml_factor_optimization_v1` |
| 创建日期 | 2026-10-07 |
| 对应 Spec | [Alpha158 机器学习因子优化研究 v1](../specs/alpha158_ml_factor_optimization_v1.md) |
| 首期目标 | 建立独立、可复用、只读正式账户的 ML 研究链，并完成 26 因子模型挑战 |
| 后续目标 | 在长历史快照完成后，验证 `all_mean_rank` 受约束分数组合 |

## 1. 交付目标

本计划交付的不是自动调参和自动部署系统，而是一套受控研究能力：

- 策略通过适配器提供 as-of 特征、成熟标签和预测股票池；
- 模型通过统一插件训练并输出严格时间顺序 OOS 分数；
- 所有分数进入现有组合、风险和执行器；
- 实验在运行前注册，参数、数据和代码均有哈希；
- 历史决定由冻结门槛机械生成；
- 研究进程不能写正式模拟盘、企业微信通知或既有 shadow；
- 历史通过最多获得独立前瞻 shadow 注册资格。

首期正式比较：

```text
26 因子 Ridge
vs ElasticNet
vs LightGBM LambdaRank
```

第二个独立实验：

```text
all_mean_rank 七族等权
vs 非负、权重和为 1、向等权收缩的线性组合
```

## 2. 当前基础与缺口

### 2.1 可复用能力

| 能力 | 现有实现 | 复用方式 |
|---|---|---|
| PIT 日线面板和股票池 | `SelectionPanelV2` | 只读加载冻结快照 |
| 26 因子和标签 | `ABuAlpha158Lite` | 通过适配器暴露，不复制公式 |
| 时间隔离 | `PurgedWalkForward` | 复用并增加成熟标签审计 |
| 组合与事件退出 | Alpha158 低换手和 `event_exit_only` | 固定版本调用 |
| 风险与成交 | `PortfolioRiskEngine`、`PortfolioExecutor` | 所有实验臂共用 |
| 统计基础 | `ABuResearchStatistics` | 扩展同步双曲线区块评估 |
| 前瞻协议 | `alpha158_forward_shadow_v1` | 历史通过后另行注册 |

### 2.2 需要新增

- 通用策略 ML 适配器；
- 训练、诊断、交易预测分离的数据集合同；
- 模型插件和模型产物清单；
- 严格 OOS 分数仓库；
- 预登记和实验预算；
- 同步双账户路径重建的区块 Bootstrap；
- 冻结门槛决策器；
- `all_mean_rank` 七族 OOS 分数适配器与受约束组合器。

### 2.3 依赖现状

当前项目虚拟环境已检测到：

```text
numpy 1.23.5
pandas 1.5.3
scikit-learn 1.2.2
scipy 1.10.1
lightgbm 未安装
```

开发过程禁止脚本自动安装 LightGBM。M4 开始前必须显式选择并冻结兼容版本，记录 wheel、平台、
编译方式和版本哈希；依赖未准备好时 M4 标记阻塞，不得回退为其他树模型。

## 3. 工程结构

计划新增：

```text
abupy/MLBu/
  __init__.py
  ABuMLContracts.py
  ABuMLDataset.py
  ABuMLStrategyAdapter.py
  ABuMLModelPlugin.py
  ABuMLOOSPredictionStore.py
  ABuMLPortfolioEvaluator.py
  ABuMLExperimentRegistry.py
  ABuMLDecision.py

abupy/MLBu/adapters/
  ABuAlpha158MLAdapter.py
  ABuAllMeanRankMLAdapter.py

abupy/MLBu/models/
  ABuRidgePlugin.py
  ABuElasticNetPlugin.py
  ABuLambdaRankPlugin.py
  ABuSimplexCombiner.py

configs/ml/
  model_registry_v1.json

configs/selection/
  alpha158_ml_factor_optimization_v1.json
  alpha158_all_mean_ml_combiner_v1.json

scripts/
  register_ml_experiment.py
  run_ml_experiment.py
  audit_ml_experiment.py
  report_ml_experiment.py
  validate_alpha158_ml_factor_optimization_v1.py
  validate_alpha158_all_mean_ml_combiner_v1.py

tests/
  test_ml_contracts.py
  test_ml_dataset.py
  test_ml_label_maturity.py
  test_ml_walk_forward.py
  test_ml_registry.py
  test_ml_ridge_parity.py
  test_ml_elastic_net.py
  test_ml_lambdarank.py
  test_ml_portfolio_evaluator.py
  test_ml_decision.py
  test_ml_all_mean_combiner.py
```

实际实现可以在不牺牲边界的前提下合并小文件，但不得新建第二套行情、风险、成交或账户引擎。

## 4. 实验状态机

每个实验只能按以下状态迁移：

```text
DRAFT
-> REGISTERED
-> BASELINE_REPRODUCED
-> RUNNING
-> HISTORICAL_SCREENED
-> REJECTED / INCONCLUSIVE / SHADOW_ELIGIBLE
-> FORWARD_REGISTERED
-> FORWARD_RUNNING
-> MANUAL_REVIEW
```

约束：

- `REGISTERED` 后配置和输入哈希不可修改；
- 基线未复现不能训练候选模型；
- `SHADOW_ELIGIBLE` 不自动创建或启动 shadow；
- 无自动切换正式策略、模拟盘或实盘的状态；
- 失败后修改参数必须创建新实验版本并计入研究尝试数量。

## 5. 里程碑总览

| 里程碑 | 内容 | 依赖 | 主要退出条件 |
|---|---|---|---|
| M0 | 合同、配置和依赖冻结 | 无 | Spec、JSON schema、依赖策略一致 |
| M1 | 通用数据集、适配器和注册表 | M0 | as-of、成熟标签、预测股票池严格分离 |
| M2 | Alpha158适配器和Ridge基线复现 | M1 | 预测、排名、订单、成交、净值满足冻结容差 |
| M3 | ElasticNet插件 | M2 | 网格只用验证集、确定性和收敛测试通过 |
| M4 | LambdaRank插件 | M2、依赖冻结 | 等级、query、权重、早停和确定性测试通过 |
| M5 | 账户评估与决策器 | M2 | 三档成本、同步区块、集中度和状态机通过 |
| M6 | 26因子正式历史筛选 | M3、M4、M5 | 完整产物、审计和机械决定完成 |
| M7 | 长历史七族OOS分数生产 | M1、长历史快照 | 七族来源manifest、成熟时间和键覆盖审计通过 |
| M8 | `all_mean_rank`受约束组合 | M5、M7 | A0/A1共同区间、对照身份和外层时间隔离通过 |
| M9 | 前瞻shadow注册材料 | M6或M8通过 | 只生成待人工确认的注册包 |

## 6. M0：合同、配置和依赖冻结

### 6.1 工作项

#### M0-T01 修订并冻结 Spec

- 落实同步双账户 Bootstrap；
- 区分训练、诊断和交易预测；
- 冻结 LambdaRank 等级函数、日期权重和早停指标；
- 冻结集中度和敏感性状态映射；
- 明确 `all_mean_rank` 是独立实验族。

#### M0-T02 定义配置 schema

配置至少包含：

```text
experiment_id
experiment_family_id
experiment_type
strategy_adapter
baseline_arm_id
candidate_arm_ids
baseline_model
candidate_models
feature_view_id
label_contract_id
walk_forward
parameter_budget
portfolio_policy_id
execution_policy_id
risk_policy_id
cost_scenarios
decision_gates
output_root
```

`baseline_arm_id` 是统计、账户和前瞻流程使用的唯一对照身份；模型替换实验指向 Ridge，组合
实验指向 A0。不得由运行脚本根据实验类型重新推断或硬编码对照。

未知字段、缺字段、重复实验 ID 和未冻结参数必须拒绝。

#### M0-T03 冻结依赖策略

- 记录当前 numpy、pandas、scikit-learn、scipy；
- 评估并选择一个与 Python 和现有二进制环境兼容的 LightGBM 版本；
- 不在研究脚本中调用 pip；
- 依赖变化必须产生新的 environment manifest。

### 6.2 自测

- 配置规范化后重复计算得到相同 SHA256；
- 任一配置字段变化导致哈希变化；
- 未注册模型或策略适配器拒绝运行；
- LightGBM 不可用时只报告依赖缺失，不回退；
- `git diff --check` 和文档链接检查通过。

### 6.3 完成条件

- Spec 不再有未定义的决定性门槛；
- 配置 schema 和状态机能够表达两个独立实验族；
- 依赖安装与研究运行解耦。

## 7. M1：通用数据集、适配器和注册表

### 7.1 工作项

#### M1-T01 实现核心合同

定义不可变对象：

```text
FeatureView
LabelContract
MLResearchDataset
ModelSpec
FoldManifest
ModelSourceManifest
OOSPredictionRecord
ExperimentRegistration
ExperimentDecision
```

#### M1-T02 实现样本掩码

每条数据明确保存：

- `feature_eligible_asof`；
- `label_mature`；
- `label_valid`；
- `diagnostic_valid`。

训练、验证、诊断和交易预测分别调用显式接口，禁止共用一次 `dropna(target)` 后的数据表。

#### M1-T03 实现实验注册表

- 注册文件先于结果生成；
- 保存代码、配置、数据和依赖哈希；
- 独立输出目录禁止覆盖；
- 保存同实验族既往尝试列表；
- 研究账户路径加入保护清单。

#### M1-T04 实现 OOS 分数仓库

写入时校验：

```text
train_label_available_at_max < validation_start_asof
validation_label_available_at_max < prediction_block_start_asof
model_available_at <= signal_asof
source_manifest父哈希链完整
fold 不重叠
strategy_id/model_id/feature_view_id/label_contract_id 完整
signal_asof + symbol + model_id 唯一
```

训练前生成并冻结完整 `expected_prediction_keys` 及哈希。写入完成后实际键集合必须完全相等；
任一必需折失败、缺键、多键或静默回退时进入 `INCONCLUSIVE_INCOMPLETE_COVERAGE`，不得汇总
部分区间结果。

### 7.2 自测

- 修改未来标签不改变当日特征和预测股票池；
- 测试尾部20日无标签仍保留评分行；
- 未成熟标签不能进入训练和诊断；
- 重复 OOS 键、标签成熟时间越界、模型选择/早停越界和折重叠拒绝写入；
- 训练信号日期合法但训练标签或验证标签越界时仍拒绝写入；
- 篡改任一父 manifest、删除父节点或制造预期键缺口时审计失败；
- 注册后修改配置或数据清单导致审计失败；
- 正式账户、通知和既有 shadow 文件在测试前后哈希不变。

### 7.3 完成条件

- 一个 Mock 策略能够完整注册、生成数据集、写入 OOS 分数并审计；
- 无模型或组合逻辑进入正式账户路径。

## 8. M2：Alpha158适配器与Ridge基线复现

### 8.1 工作项

#### M2-T01 实现 `ABuAlpha158MLAdapter`

- 直接调用现有26因子和标签公式；
- 暴露训练、验证、测试预测和诊断视图；
- 保留日期等权和训练期中位数填充；
- 不复制、重写或近似标签可执行性规则。

#### M2-T02 实现 Ridge 插件

- 固定 `alpha=100`；
- 输出训练日期、填充值、系数和依赖版本；
- 生成与现有实现相同的 OOS 分数。

#### M2-T03 基线账户重放

固定：

```text
strategy = alpha158_price_only_no_rank_exit_v1
policy = alpha158_lite_low_turnover_v3
review_overlay = suppress_rank_exits
execution = pit_corrected
```

### 8.2 自测

- 样本键和标签版本哈希完全一致；
- Ridge 分数最大绝对误差不超过 `1e-12`；
- 每日排名、订单和成交业务字段一致；
- 每日现金和净值误差不超过0.01元；
- 未来测试标签变化不改变分数；
- 重复运行产物哈希一致。

### 8.3 完成条件

- 状态进入 `BASELINE_REPRODUCED`；
- 任一容差失败则停止 M3/M4，不生成候选结论。

## 9. M3：ElasticNet插件

### 9.1 工作项

- 实现冻结的12组 `alpha/l1_ratio` 小网格；
- 使用与Ridge相同的训练矩阵和日期权重；
- 只用验证集日期等权MSE选择参数；
- 保存每折参数、非零系数、符号和稳定性；
- 网格全部不收敛时该折失败关闭。

### 9.2 自测

- 测试集标签变化不影响参数选择；
- 并列验证误差按更大alpha、更大l1_ratio稳定决胜；
- 相同输入重复训练得到相同系数和预测；
- 零系数只进入报告，不触发特征删除；
- 不收敛和NaN系数得到明确失败状态。

### 9.3 完成条件

- 所有预登记必需折生成完整且唯一 OOS 预测；任一折失败时整条候选不得历史通过；
- 模型产物可以在独立进程加载并复现分数。

## 10. M4：LambdaRank插件

### 10.1 前置条件

- LightGBM兼容版本已显式安装并写入依赖锁；
- 不允许研究脚本自动安装或切换实现。

### 10.2 工作项

#### M4-T01 实现等级编码

- 使用平均未来收益名次；
- 后50%分支优先；
- 覆盖半整数、并列、小query和全并列；
- 退化query记录原因并从训练/早停排除。

#### M4-T02 构造query、训练权重和验证指标权重

```text
w(q, i) = N / (Q * n_q)
```

- 行按日期稳定排序；
- 训练 `group`、label、weight 和特征严格对齐；
- 每个训练 query 输入权重和相等；
- 验证 Dataset 不传逐行权重，NDCG 对日期 query 等权聚合；
- 用手工逐日 NDCG 算术平均值对账原生指标；
- 报告query规模与模型贡献诊断。

#### M4-T03 训练和早停

- 使用Spec冻结参数；
- 显式 `first_metric_only=true`；
- 第一个验证指标固定为 `NDCG@10`；
- 保存最佳迭代数和模型文本哈希。

### 10.3 自测

- 每个成熟有效标签恰好映射一个等级；
- 10.5、20.5、50.5、100.5边界行为固定；
- 100只、150只和全并列query没有区间重叠；
- 退化query不参与训练，但当日所有as-of合格股票仍获预测；
- 每个训练 query 权重和误差不超过 `1e-12`；
- 大小不同的验证 query 获得相同日期权重，原生 NDCG 与手工聚合误差不超过 `1e-12`；
- NDCG@20/50停滞而NDCG@10改善时不早停；
- 同依赖、线程和输入重复训练哈希一致。

### 10.4 完成条件

- 全部有效折生成唯一OOS预测；
- query、等级、权重和早停审计全部通过。

## 11. M5：账户评估、统计和决策器

### 11.1 工作项

#### M5-T01 统一组合回放

- 三模型使用同一个评分转排名入口；
- 完整重放25/40/60bp单边滑点和额外费用；
- 每档成本独立运行资金、风险和成交路径。

#### M5-T02 同步双曲线区块Bootstrap

- 从注册读取 `baseline_arm_id`，保存候选、注册对照两列对齐收益；
- 相同区块索引同步抽样；
- 分别重建净值；
- 分别计算累计收益、CAGR、回撤和ES；
- 最后计算指标差；
- 20日主区间，40/60日敏感性。

#### M5-T03 集中度和状态机

- 复用Top5正利润占比公式和50%门槛；
- 行业集中只报告，不做硬门槛；
- 20日主区间参与状态映射；40/60日只报告区间、宽度和分布中位数；
- 缺指标进入 `INCONCLUSIVE`，不按通过处理；
- 不计算Holm p值，不声称多重比较已被控制。

### 11.2 自测

- 人工构造两账户收益，证明差收益曲线与分别重建结果不同时使用后者；
- 账户完全相同时全部指标差为零；
- 调换账户后指标差符号反转；
- 非线性最大回撤和ES使用各自路径；
- 无正利润、恰好50%和超过50%的集中度边界正确；
- 每组门槛输入映射到唯一状态；
- A1实验从注册到报告全程只能解析A0，调用Ridge对照时失败；
- 报告不能通过命令行覆盖决定状态。

### 11.3 完成条件

- Mock、Ridge和一个伪候选均能完成三档成本报告；
- 决策完全由注册配置和产物生成。

## 12. M6：26因子正式历史筛选

### 12.1 运行前

- 工作区提交状态和是否干净写入注册；
- 固定数据清单、代码、配置、依赖和输出目录；
- 登记此前相关模型和因子实验；
- 先审计 Ridge 基线复现。

### 12.2 正式运行

- 统一区间为2023-07-27至2026-09-30；
- 顺序运行Ridge、ElasticNet、LambdaRank；
- 生成因子诊断、账户、年度、风险、压力和区块报告；
- 对M1、M2分别生成机械决定。

### 12.3 自测与审计

- OOS键无重复、折无重叠；
- 测试尾部标签缺失不影响账户日期；
- 三模型预测股票池相同；
- 全部产物哈希可复算；
- 正式账户和既有shadow哈希不变；
- 第二次完整重放结果逐字节或在冻结浮点容差内一致。

### 12.4 完成条件

- 输出 `REJECTED`、`INCONCLUSIVE` 或 `SHADOW_ELIGIBLE`；
- 不根据结果继续修改v1参数。

## 13. M7：长历史七族OOS分数生产

### 13.1 前置条件

- 2015--2026行情分片完成合并与审计；
- 冻结新的长历史数据manifest；
- 2012--2014财务预热和非严格PIT财务因子不进入输入。

### 13.2 工作项

- 冻结七个家族各自的特征、标签、滚动折、模型和依赖配置；
- 运行2015--2026七族逐折历史OOS预测；
- 为每折输出训练和验证标签最后成熟时间、模型选择/早停时间及父manifest哈希；
- 递归审计所有来源manifest；
- 预登记并核对每个家族的预期预测键，不允许失败折被删除；
- 按“七族首个完整OOS日期与执行数据首个完整日期取较晚者”冻结共同评价起点；
- 冻结共同区间和共同预期预测键，缺任一家族键即失败关闭，不在运行后静默取交集。

### 13.3 自测

- 训练信号日期合法但标签成熟或调参时间越界时拒绝来源；
- 任一父manifest缺失或哈希变化时递归审计失败；
- 任一家族失败折导致阶段不完整，不缩短区间继续运行；
- 共同区间只由完整性和执行可用性决定，不读取收益结果；
- 重复运行得到相同预测键、来源manifest和哈希。

### 13.4 完成条件

- 七族长历史OOS来源可递归审计；
- A0/A1共同区间和预期键已冻结；
- 未满足时M8不得开始。

## 14. M8：`all_mean_rank`受约束组合实验

### 14.1 前置条件

- M7七族OOS来源和共同区间审计通过；
- A0与A1的注册均固定 `baseline_arm_id=all_mean_rank_a0`；
- A0/A1初始资金、起始日期和空仓状态相同。

### 14.2 工作项

- 实现 `ABuAllMeanRankMLAdapter`；
- 复现七族等权 `all_mean_rank`；
- 实现非负、权重和为1、向等权收缩的线性组合器；
- 只用更早日期的家族OOS分数训练；
- 使用Spec冻结的 `lambda=[0.01, 0.10, 1.00, 10.00]`，验证误差并列时选择更大lambda；
- 冻结优化器、收敛容差和最大迭代数，不收敛时失败关闭；
- 以等权A0为对照运行统一账户评估。

### 14.3 自测

- 七个输入权重非负且和为1；
- 极大收缩时权重回到1/7；
- 测试日期变化不影响更早权重；
- 任一家族分数没有OOS来源时日期失败关闭；
- 不允许读取26个原始因子或财务敏感性结果；
- A0/A1从共同日期、相同资金和空仓状态启动，实际评价日期与预测键完全一致；
- 注册、Bootstrap、报告、决定和shadow材料不得解析Ridge为A1对照；
- A0账户复现当前冻结结果或解释长历史区间差异。

### 14.4 完成条件

- A1相对A0独立判定；
- 不与M1/M2择优后合并成一个历史冠军。

## 15. M9：前瞻shadow注册材料

只有 `SHADOW_ELIGIBLE` 候选进入本阶段。

### 15.1 工作项

- 生成双账户genesis：注册 `baseline_arm_id` 对照与一个候选；
- 保存模型、代码、依赖、来源manifest链、训练和验证标签最后成熟时间及数据哈希；
- 空仓、等资金、等数据和等执行起步；
- 复用6/9/12/18个月及至少30个买入日期簇协议；
- 与现有 `all_mean_rank` 研究或shadow保持独立命名空间。

### 15.2 自测

- 重复注册拒绝覆盖；
- 注册后修改模型或配置导致运行拒绝；
- 研究账户无法发送正式订单或企业微信交易通知；
- 漏日、数据冲突和公司行为异常失败关闭；
- 提前评估只返回未到期状态。
- A1的shadow genesis对照必须是A0；M1/M2的shadow genesis对照必须是Ridge。

### 15.3 完成条件

- 只生成待人工确认的注册包；
- 不自动启动定时任务，不切换正式策略。

## 16. 每阶段交付要求

每个里程碑完成后必须提交：

```text
代码和配置
单元测试及结果
一份 docs/reviews/ 下的阶段评审
已知限制
输入和输出哈希
下一阶段是否满足前置条件
```

阶段评审必须区分：

- 功能正确；
- 基线可复现；
- 历史指标改善；
- 前瞻准入。

前两项通过不代表后两项通过。

## 17. 停止条件

出现以下任一情况时停止当前实验族，不进入下一模型搜索：

- Ridge基线无法复现；
- as-of候选被未来标签或未来成交条件过滤；
- OOS分数存在训练、验证标签成熟时间、模型选择时间或递归来源违规；
- 任一必需折预测覆盖不完整；
- 配置或数据在注册后变化；
- 模型依赖不可复现；
- 正式账户或既有shadow被修改；
- 冻结候选失败后需要扩大网格或修改标签才能继续。

停止不影响通用研究平台继续服务其他已经独立注册的策略实验。
