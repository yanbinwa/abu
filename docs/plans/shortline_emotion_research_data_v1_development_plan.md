# A 股短线情绪、题材与超预期研究数据层 v1 开发计划

| 字段 | 内容 |
|---|---|
| 状态 | Proposed；等待实施 |
| 计划版本 | 0.1.0 |
| 创建日期 | 2026-10-03 |
| 对应 Spec | [A 股短线情绪、题材与超预期研究数据层 v1 Spec](../specs/shortline_emotion_research_data_v1.md) |
| 基线提交 | `e107288`；实施开始前重新确认 |
| 基线数据快照 | `sha256:41e541057ccce0d0a136420912cd9efdc15ac5d5f4638420529f08f27151ad9c` |
| 当前阶段 | 设计完成，尚未实施 |

## 1. 计划目标

本计划把 Spec 0.2.0 转换为可顺序实施、测试和审查的工程任务。最终交付包括：

- 可审计的涨跌停参考价和历史制度状态机；
- 证券级 `daily_limit_facts`；
- 分市场、分板块的规则和参考价覆盖报告；
- 物理隔离的开盘/收盘情绪特征；
- 免费外部事件源的原始快照、可获得性证据和冲突审计；
- 双时态题材 taxonomy、题材强度和龙头相对特征；
- 分离的预期预测、实际结果和 surprise 数据集；
- 独立的短线交易策略 Spec 与实验预登记；
- 通过统一风险、执行器和 placebo v2 的消融实验入口。

本计划不承诺新因子盈利。M0—M7 的交付目标是数据正确性和研究可复现性；M8—M9 才允许验证风险覆盖和选股假设。

## 2. 工作原则

### 2.1 先修事实，再生成特征

涨跌停参考价和制度规则是所有下游特征的共同依赖。M1 未通过前，不生成正式连板、晋级率、情绪状态、题材强度或龙头评分。

### 2.2 旧结果只读保留

修正参考价和价格限制可能改变现有执行器的成交路径。必须：

- 保留现有 `pit_corrected` 输出和配置；
- 为修正后的执行语义增加新版本；
- 不在原输出目录重跑覆盖；
- 对交易差异逐笔输出原因。

### 2.3 数据层与策略层分开

M0—M7 不以收益优化作为完成条件。M8 必须先冻结单独的短线策略 Spec 和实验注册表，之后才能查看新策略收益。

### 2.4 事实、特征、标签物理隔离

存储和加载接口必须让以下错误在结构上难以发生：

- 策略加载器读取未来收益；
- 预测训练读取预测期之后的样本；
- 当前题材 taxonomy 回填历史；
- 历史查询结果伪装成当时实时可见数据。

### 2.5 失败关闭和显式降级

未知参考价、未知价格制度、未知 ST、未知题材可用时间均不得填成有利值。降级结果必须携带原因码，低覆盖日的市场状态为 `UNKNOWN`。

### 2.6 一次只改变一个研究变量

M9 的实验顺序固定为：情绪风险覆盖、题材筛选、龙头排序、日线超预期、精细事件增强。不得一次混合所有新因素后再倒推贡献。

## 3. 范围和依赖

### 3.1 复用依赖

- `SelectionPanelV2` 的 PIT 股票池和字段掩码；
- 原始/复权行情、成交额、换手率、流通市值；
- `TradeIntent`、组合风险引擎和统一执行器；
- 数据快照、placebo v2 和研究统计；
- 现有历史行业作为明确标记的 `industry_proxy`。

### 3.2 必须修复的依赖

- 原始行情未保留 `pre_close/reference_price`；
- 执行器以最后有效原始收盘价计算当日涨跌停；
- `ABuPriceLimit` 在判断板块前把全部 ST 映射为5%；
- 退市整理、重新上市和特殊交易状态不完整；
- 上海历史 ST 状态覆盖不足。

### 3.3 外部依赖

- AKShare/东方财富近期事件池；
- eltdx/通达信公开协议的事件、竞价和题材数据；
- 可选辅助校验源；
- 交易所规则和可获得的证券参考数据。

所有外部依赖都必须允许禁用；禁用后 M1—M4 核心日线链路仍应运行。

## 4. 建议目标结构

```text
abupy/AlphaBu/
  ABuLimitReference.py
  ABuPriceLimit.py
  ABuShortLineEvents.py
  ABuMarketEmotion.py
  ABuThemeStrength.py
  ABuExpectationModel.py

abupy/MarketBu/
  ABuDataFeedEltdx.py
  ABuDataFeedShortLineAkShare.py

configs/selection/
  shortline_data_v1.json
  emotion_state_v1.json
  emotion_risk_overlay_v1.json
  shortline_experiment_registry_v1.json

scripts/
  audit_limit_reference.py
  build_daily_limit_facts.py
  collect_shortline_snapshots.py
  normalize_shortline_events.py
  build_market_emotion_features.py
  audit_shortline_coverage.py
  freeze_shortline_snapshot.py

tests/
  test_limit_reference.py
  test_price_limit_regimes.py
  test_daily_limit_facts.py
  test_shortline_event_normalization.py
  test_market_emotion_features.py
  test_theme_strength.py
  test_expectation_model.py
  test_shortline_pit.py
  test_feature_outcome_isolation.py
```

历史快照不原地改写。参考价和短线数据优先使用 sidecar 数据集：

```text
selection_research/
  limit_reference/
  shortline_raw/
  shortline_normalized/
  shortline_features/
```

## 5. 里程碑总览

| 里程碑 | 内容 | 依赖 | 阻断门禁 |
|---|---|---|---|
| M0 | 冻结修复前基线 | 无 | 代码、数据和已知缺陷可复现 |
| M1 | 参考价与价格限制状态机 | M0 | 两个 P0 全部修复 |
| M2 | 证券级日线涨跌停事实 | M1 | 事实可追溯、未知不误标 |
| M3 | ST 补全与覆盖门槛 | M2 | 分市场覆盖清晰，低覆盖状态 UNKNOWN |
| M4 | 开盘/收盘情绪特征 | M3 | as-of 特征物理隔离 |
| M5 | 免费源和可获得性审计 | M0 | 历史回补不伪装实时可见 |
| M6 | 每日增量采集与题材双时态 | M5 | 原始快照不可变，taxonomy 不回填 |
| M7 | 题材、龙头和超预期研究集 | M4、M6 | 特征、预测和结果物理隔离 |
| M8 | 策略 Spec、预登记和 shadow | M7 | 看收益前冻结主假设 |
| M9 | 选股消融和统一验证 | M8 | 同执行路径、placebo 和统计门禁 |

可并行路径：

```text
M0 -> M1 -> M2 -> M3 -> M4 --\
  \-> M5 -> M6 ---------------> M7 -> M8 -> M9
```

M1 是核心事实路径阻断项，M5 可以在 M1 开发期间独立进行只读覆盖审计。

## 6. M0：冻结修复前基线

### 6.1 任务

#### M0-T1 记录代码和工作树

- 记录 HEAD、分支和工作树状态；
- 登记与本项目无关的未提交文件，实施时不修改；
- 将本 Spec 和开发计划纳入版本管理；
- 记录 Python、pandas、numpy、AKShare 等依赖版本。

#### M0-T2 冻结数据和旧结果

- 复用现有 `snapshot_manifest.json`；
- 保存现有 v2 交易、曲线和执行配置哈希；
- 登记参考价和 ST 规则已知缺陷；
- 不将旧涨跌停判断作为正确事实基线。

#### M0-T3 建立缺陷基线样本

选取并固定：

- 普通交易日；
- 现金分红除息日；
- 送转除权日；
- 创业板 ST；
- 科创板 ST；
- 2026-07-06前后主板 ST；
- 新股前五日；
- 退市整理首日；
- 重新上市首日；
- 长期停牌后恢复交易。

保存当前实现对这些样例的输出，作为修复差异审计输入，不作为正确答案。

### 6.2 测试

- 相同输入生成相同快照 ID；
- 基线样例列表包含日期、证券和预期核验来源；
- 当前工作树的无关修改未被覆盖。

### 6.3 完成条件

- 修复前状态可以定位；
- 已知错误不会被误写成正式基线；
- 后续输出使用新目录和新 schema 版本。

## 7. M1：参考价与价格限制状态机

### 7.1 任务

#### M1-T1 参考价字段审计

对当前 Sina、腾讯、AKShare 和候选交易所参考数据检查：

- `pre_close/reference_price` 是否存在；
- 是否为除权除息后的行情前收盘；
- 是否直接提供涨停价、跌停价和无涨跌幅标志；
- 历史覆盖、空值率和 schema 稳定性；
- 数据最早可获得时间和历史查询语义。

输出 `limit_reference_source_audit.md` 和覆盖表。在来源未确认前，不开始批量重建。

#### M1-T2 建立参考价 sidecar

新增 `ABuLimitReference.py`，定义：

```text
LimitReference
  trade_date
  symbol
  previous_raw_close
  limit_reference_price_raw
  exchange_upper_limit_raw
  exchange_lower_limit_raw
  source
  quality
  effective_at
  available_at
  availability_evidence
  reason_codes
```

处理优先级：直接交易所上下限、可信 `pre_close`、完整公司行为重建、UNKNOWN。不得自动回退到昨日收盘。

#### M1-T3 扩展价格制度状态机

保持旧 API 的兼容入口，新增版本化上下文：

```text
LimitRuleContext
  exchange
  board
  trade_date
  security_status
  listing_stage
  delisting_stage
  special_trading_event
  status_known
```

实现 Spec 规定的制度矩阵。规则数据本身与代码分离或以显式版本表表达，避免继续累积无法审计的条件分支。

#### M1-T4 修复执行器但保留旧模式

- 旧模式继续复现历史结果；
- 新执行模式读取 `LimitReference`；
- UNKNOWN 时执行风险可以使用保守 fallback，但事实标签保持 UNKNOWN；
- 输出 `execution_limit_model_version`；
- 对所有成交差异给出 `REFERENCE_PRICE_CHANGED`、`LIMIT_REGIME_CHANGED` 等原因。

#### M1-T5 冻结修复后快照

- 生成新的数据和代码哈希；
- 更新覆盖报告；
- 保存修复前后差异，不覆盖旧报告。

### 7.2 原因码

至少包括：

```text
UNKNOWN_REFERENCE_PRICE
DIRECT_EXCHANGE_LIMIT
PROVIDER_PRE_CLOSE
CORPORATE_ACTION_RECONSTRUCTION
REFERENCE_SOURCE_CONFLICT
UNKNOWN_SECURITY_STATUS
NO_DAILY_LIMIT
LIMIT_REGIME_FALLBACK
REFERENCE_PRICE_CHANGED
LIMIT_REGIME_CHANGED
```

### 7.3 测试

- 除权日参考价不等于昨日实际收盘时上下限正确；
- 送转和现金分红样例正确；
- 创业板注册制前后普通及 ST 规则正确；
- 科创板 ST 保持相应板块比例；
- 主板 ST 在2026-07-06前后使用对应规则；
- 新股、退市整理首日、重新上市首日的无涨跌幅状态正确；
- `status_known=False` 不生成确定性事实；
- 旧执行模式结果保持不变；
- 新执行模式的全部差异有原因码。

### 7.4 完成条件

- 两个 CR P0 均关闭；
- 规则表和参考价都可单独审计；
- 执行器不再无条件依赖最后有效原始收盘；
- 修复后的基线和数据快照已冻结。

## 8. M2：证券级日线涨跌停事实

### 8.1 任务

#### M2-T1 构建 `daily_limit_facts`

- 使用整数分价比较；
- 生成 touched/closed/opened upper/lower；
- 生成近似炸板和一字板；
- 保存参考价、规则、来源、质量和原因码；
- 无价格限制或事实未知时使用 nullable 状态。

#### M2-T2 连板与窗口事件

- 计算严格连续封板；
- 停牌、未知状态和非封板中断严格连板；
- 另存3/5/10日涨停次数；
- 对供应商连板数只做对账。

#### M2-T3 晋级输入和炸板分组

- 保存昨日板位集合；
- 区分正常失败、停牌、终止上市、状态未知和行情缺失；
- 生成全部触板和排除一字板的炸板口径；
- 按交易所和板块保留分子、分母。

#### M2-T4 外部重叠日期核验

在 AKShare/eltdx 可用日期比较：

- 收盘封板集合；
- 跌停集合；
- 供应商板位；
- 不一致记录及可能原因。

外部列表不覆盖本地事实，冲突进入审计表。

### 8.2 测试

- OHLC 边界和分价四舍五入；
- 无涨跌幅日期字段为空；
- UNKNOWN 不被转换为 false；
- 停牌对严格连板的影响；
- 分母为零时晋级率输入保持空；
- 相同快照重复构建哈希一致；
- 修改未来行情不改变历史事实。

### 8.3 完成条件

- 2020年以来可确定样本全部生成；
- 任一标签可追溯到参考价、规则和原始 OHLC；
- 外部冲突有完整报告；
- 不生成市场情绪状态。

## 9. M3：ST 补全与覆盖门槛

### 9.1 任务

#### M3-T1 上海 ST 覆盖审计

- 按证券和日期统计已知/未知；
- 区分“明确非 ST”和“没有历史记录”；
- 检查名称变更、交易所状态和候选历史来源；
- 不把当前名称回填历史。

#### M3-T2 历史状态补全

- 优先使用带生效日期的交易所或公告记录；
- 保存来源和 availability evidence；
- 冲突状态进入人工核验清单；
- 无证据日期继续保持 UNKNOWN。

#### M3-T3 覆盖门槛

按日、交易所、板块生成：

```text
full_eligible_count
known_rule_count
unknown_rule_count
known_reference_price_count
coverage_ratio
```

实现冻结的 `minimum_limit_fact_coverage=0.95`。覆盖不足只阻止正式状态和主策略特征，不删除事实记录。

### 9.2 测试

- 未知上海证券不会默认为非 ST；
- 同一证券 ST 进入/退出边界正确；
- 覆盖不足状态为 UNKNOWN；
- 沪市缺失不会被深市完整度掩盖；
- 分市场合计与全样本一致。

### 9.3 完成条件

- 覆盖率按日和板块可见；
- 未达门槛的序列不称为全市场；
- 所有补全记录都有来源和生效日期。

## 10. M4：开盘/收盘情绪特征

### 10.1 任务

#### M4-T1 开盘 as-of 表

构建 `market_emotion_asof_open`，只包含当日开盘后可知信息，例如昨日涨停股开盘收益和正溢价率。

#### M4-T2 收盘 as-of 表

构建 `market_emotion_asof_close`，包含当日涨跌停数量、连板分布、炸板近似、晋级率、市场广度和覆盖。

#### M4-T3 双晋级率和分组炸板率

- `unconditional_promotion_rate`；
- `tradable_promotion_rate`；
- 全触板炸板率；
- 排除一字板炸板率；
- 交易所和板块分组结果。

#### M4-T4 情绪状态接口

首期只提供连续特征和 `UNKNOWN` 门禁。若建立状态规则：

- 单独配置和版本化；
- 只使用滚动历史阈值；
- 不在本里程碑查看策略收益；
- 输出每个状态的触发原因。

### 10.2 测试

- open 特征表不存在收盘字段；
- close 特征表不存在未来开盘收益；
- 特征加载器不能打开 forward outcome 表；
- 修改未来数据不改变历史特征；
- 分子、分母和覆盖率可复算。

### 10.3 完成条件

- 每个特征都有明确 decision time；
- 不存在一行一个含混 `available_at` 的宽表；
- 任一聚合值可以下钻到证券级事实。

## 11. M5：免费源与可获得性审计

### 11.1 任务

#### M5-T1 eltdx 只读覆盖探测

抽样2022—2026年的：

- 涨跌停列表；
- 首封和最后封板时间；
- 开板次数；
- 涨停原因；
- 历史竞价；
- 题材成员和连板天梯。

只记录覆盖、字段、错误和响应语义，不把结果直接加入策略数据。

#### M5-T2 AKShare 近期事件池审计

- 确认各接口可查询的最早日期；
- 区分零事件和历史不保留；
- 保存当前 schema；
- 与本地事实核验重叠日期。

#### M5-T3 可获得性证据分类

为每个数据集和字段指定：

- 字段类别；
- availability evidence；
- 是否允许进入历史 as-of 特征；
- 是否只允许作为标签或事件研究；
- 是否只允许从当前开始前瞻采集。

#### M5-T4 适配器契约

定义原始快照元数据、schema hash、错误码、空响应语义和限流。适配器不向策略直接返回 DataFrame，只写原始快照或规范化事件。

### 11.2 测试

- 网络关闭时核心日线链路正常；
- 空响应不等于零事件；
- schema 变化停止规范化；
- 相同 payload 规范化幂等；
- `BACKFILLED_QUERY` 分类字段不能进入 as-of 加载器；
- 限流和重试有上限。

### 11.3 完成条件

- 字段×日期×来源×证据覆盖矩阵完成；
- 明确哪些数据只能用于前瞻；
- 没有将“今天能查询”解释成“历史当时可见”。

## 12. M6：每日增量采集与题材双时态

### 12.1 任务

#### M6-T1 增量采集脚本

支持独立运行：

- 09:26竞价结果；
- 15:05原始行情和参考价；
- 15:20涨停、跌停和炸板池；
- 收盘后题材、原因和连板天梯。

脚本按交易日和数据集分目录，原始响应不可覆盖。

#### M6-T2 幂等和补抓

- 相同 payload 不重复规范化；
- 失败批次可重试；
- 未知结果不写成零行成功；
- 补抓保留真实 `ingested_at` 和 `availability_evidence`。

#### M6-T3 题材 taxonomy

建立：

```text
source_theme_id
canonical_theme_id
taxonomy_version
valid_from
valid_to
mapping_available_at
```

支持改名、合并、拆分和多对多映射。历史策略使用当时可见 taxonomy；最新 taxonomy 只用于明确标记的回顾分析。

#### M6-T4 前瞻运行手册

记录交易日检查、失败补抓、schema 变更、时钟和时区、节假日及人工处理流程。本计划不自动创建定时任务，待用户另行授权。

### 12.2 测试

- 重复运行不覆盖原文件；
- 同一日多个抓取批次都可追踪；
- taxonomy 映射在 `mapping_available_at` 前不可见；
- 时区统一为 Asia/Shanghai；
- 非交易日不会写入正常零事件。

### 12.3 完成条件

- 连续模拟采集通过；
- 原始 payload 和规范化结果双向可追踪；
- 当前题材不会回填历史。

## 13. M7：题材、龙头和超预期研究集

### 13.1 任务

#### M7-T1 题材强度

- 只使用有历史可用性证据的成员关系；
- 生成成员数、涨停数、板位分布、持续性和覆盖率；
- 未来收益写入 `theme_forward_outcomes`；
- `industry_proxy` 独立输出。

#### M7-T2 龙头相对特征

- 同日同题材独立排名；
- 多题材股票保留多条排名；
- 缺失首封时间不填充最早/最晚；
- 保留评分原始分量，不在数据层冻结策略权重。

#### M7-T3 日线超预期基线

先实现 `daily_open`：

- expanding/rolling 训练；
- 预测开盘收益和晋级概率；
- 保存训练边界、模型和快照哈希；
- 不使用随机打散时间样本。

#### M7-T4 三表隔离

分别写：

- `expectation_predictions`；
- `expectation_actuals`；
- `expectation_surprises`。

只有 outcome 可见后才生成 surprise。策略特征加载器不得读取 actuals。

#### M7-T5 竞价研究门禁

只有历史覆盖、availability evidence 和执行时序审计同时通过，才建立竞价事件研究集。当前日线执行器不支持09:25决策后按09:30开盘成交。

### 13.2 测试

- 题材双时态；
- 多题材排名；
- 缺失事件字段传播；
- 训练结束早于预测结果；
- 修改未来标签不改变历史预测；
- prediction/actual/surprise 主外键完整；
- as-of 加载器无法访问结果表。

### 13.3 完成条件

- 研究数据集可生成但尚未用于收益选择；
- 每个预测可复算；
- 题材和龙头覆盖率明确；
- 竞价不足时保持 research-only。

## 14. M8：策略 Spec、实验预登记与 Shadow mode

### 14.1 任务

#### M8-T1 单独建立短线策略 Spec

与用户确认并冻结：

- 一个基础策略版本；
- 情绪状态规则；
- 决策和执行时间；
- 买入价格上限、跳空、订单期限；
- 止损、R 和退出优先级；
- T+1、停牌和连续跌停；
- RETREAT 对新仓和已有仓位的不同处理。

#### M8-T2 预登记主假设

生成 `shortline_experiment_registry_v1.json`。不得把2020—2026任一已观察区间声称为全新留出样本。登记历史 walk-forward 协议和真正前瞻起始日。

#### M8-T3 情绪风险覆盖 Shadow mode

- 不改变现有成交；
- 记录 NORMAL/CAUTION/RETREAT/UNKNOWN 下本应允许、缩量或拒绝的意图；
- 报告市场敞口、风险预算和拒绝原因；
- 首先验证数据和控制逻辑，不选择收益最优阈值。

#### M8-T4 审查后启用覆盖层

只有 shadow 覆盖率和原因码通过后，才建立新的 `emotion_risk_overlay` 配置版本。它不修改冻结的 `risk_v1`。

### 14.2 测试

- shadow 不改变任何订单和成交；
- UNKNOWN 默认不新增风险；
- RETREAT 不阻止卖单和已有退出；
- 动态风险预算有配置哈希；
- 风险覆盖和选股评分使用不同开关；
- 注册表缺失时正式回测拒绝启动。

### 14.3 完成条件

- 收益查看前已经冻结策略和检验族；
- shadow 决策可以逐单解释；
- 风险覆盖层与选股层可独立关闭。

## 15. M9：选股消融和统一验证

### 15.1 实验顺序

1. 冻结基础策略；
2. 基础策略 + 情绪风险覆盖；
3. 独立题材强度筛选；
4. 题材候选 + 龙头排序；
5. `daily_open` 超预期；
6. 覆盖完整样本上的精细事件增强；
7. 统一风险、placebo v2、压力和市场状态报告。

相邻实验只改变一个维度，共同样本实验和全覆盖实验分别报告。

### 15.2 评价口径

风险覆盖层主要报告：

- 最大回撤；
- Expected Shortfall；
- 三跌停清算回撤；
- 收益保留率；
- 平均市场敞口；
- 拒单和容量损失。

选股层主要报告：

- 成本后 R 期望及交易簇置信区间；
- 同执行路径 placebo 分位数；
- 市场和行业 beta；
- 换手、容量和滑点；
- 前几笔盈利集中度；
- 跨年度和市场状态稳定性。

### 15.3 多重检验

- 所有题材、龙头和超预期变体登记到同一检验族；
- 报告原始 p 值和 FDR q 值；
- 消融用于归因，不从中追选历史最优候选；
- 未达到上位 Spec 门槛时保持 `research_only`。

### 15.4 完成条件

- 所有实验使用同一事实快照、执行器和费用；
- placebo 匹配情绪、题材/代理、板位和流动性；
- 风险改善没有被描述为选股 alpha；
- 未通过门槛的策略不进入模拟盘候选。

## 16. 横向测试计划

### 16.1 每次提交

- 相关单元测试；
- 原有 v2 回归测试；
- `git diff --check`；
- schema 和配置严格加载测试；
- 无网络测试（适用时）。

### 16.2 每个里程碑

- 全量相关测试；
- 合成 PIT 污染测试；
- 固定真实样例对账；
- 两次构建哈希一致性；
- 覆盖、缺失和冲突报告审查；
- 里程碑 review 文档。

### 16.3 正式实验前

- 完整测试套件；
- 数据快照和代码提交冻结；
- 特征/标签隔离验证；
- 实验注册表验证；
- 费用、滑点、风险和执行配置哈希；
- placebo 因果性测试；
- 前瞻起始时间确认。

## 17. 输出与版本管理

### 17.1 目录

```text
backtests/shortline/<experiment_id>/
data/selection_research/shortline_raw/
data/selection_research/shortline_normalized/
data/selection_research/shortline_features/
```

`experiment_id` 至少包含策略版本、数据快照短哈希、配置短哈希和运行时间。

### 17.2 Schema 版本

以下对象分别版本化：

- limit reference；
- limit rule；
- daily facts；
- external events；
- taxonomy；
- emotion features；
- expectation model；
- strategy；
- risk overlay；
- execution model。

破坏性字段修改增加 schema major 版本，不在原文件上覆盖。

### 17.3 最低运行产物

沿用 Spec 第23节，另外每个里程碑保存：

```text
milestone_manifest.json
coverage_summary.json
quality_report.json
known_limitations.json
review.md
```

## 18. 主要风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 历史参考价拿不到 | 无法可靠重建涨跌停 | 保持 UNKNOWN，缩小正式样本，不用昨日收盘伪造 |
| 上海 ST 仍不完整 | 全市场情绪偏向深市 | 分市场报告、95%门槛、状态 UNKNOWN |
| 2026制度变化遗漏 | 最新样本误标 | 规则表带有效日期，固定边界测试 |
| 免费源历史很短 | 题材/竞价样本不足 | 日线核心先行，从现在持续归档 |
| 供应商字段变更 | 静默错列 | schema hash 变化即停止规范化 |
| 回补分类事后修订 | 历史前视 | availability evidence 和字段类别门禁 |
| 特征读取未来标签 | 虚假收益 | 物理拆表、独立加载器和负向测试 |
| 题材 taxonomy 回填 | 题材历史失真 | 双时态映射和 retrospective 标记 |
| 修复执行器改变旧结果 | 无法归因 | 保留旧模式、新版本和逐笔差异报告 |
| 研究自由度过大 | 过拟合 | 策略 Spec、预登记、共同样本和 FDR |
| 竞价后开盘成交不现实 | 执行前视 | 在分钟执行器完成前仅事件研究 |

## 19. 提交拆分建议

建议每个提交只包含一个可审查主题：

1. Spec、计划和基线登记；
2. 参考价对象及覆盖审计；
3. 价格限制状态机；
4. 执行器新版本和兼容回归；
5. `daily_limit_facts`；
6. ST 补全和覆盖门槛；
7. open/close 情绪特征；
8. 外部适配器和原始快照；
9. taxonomy 双时态；
10. 题材和龙头特征；
11. 预测/结果/surprise 三表；
12. 策略 Spec、注册表和 shadow；
13. placebo 匹配扩展与正式报告。

不要把参考价修复、规则修复、情绪特征和策略收益变更合并成一个提交。

## 20. 里程碑审查模板

每个里程碑完成后新增 review：

```markdown
# Milestone Mx Review

## 实现范围
## 修改文件
## 数据和 Schema 版本
## 测试结果
## 覆盖、缺失和冲突
## 与旧版本差异
## 未解决问题
## 是否满足完成条件
## 下一阶段准入结论
```

任何门禁失败时不得通过“先跑收益看看”绕过。

## 21. 需要冻结的实施前决策

M1 开始前：

- 历史参考价的主来源和备用来源；
- sidecar 文件格式；
- 新执行模型版本名称；
- 交易所制度表的维护方式。

M6 开始前：

- 每日采集数据集和运行时间；
- 题材 taxonomy 初始命名规则；
- 外部源许可证和用途边界。

M8 开始前：

- 唯一基础策略；
- 首个情绪状态规则；
- 主假设和主指标；
- 最小样本量；
- 历史 walk-forward 和前瞻起始日；
- 多重检验族。

这些决策必须落盘并进入哈希，不在看到结果后补写。

## 22. 整体完成定义

本计划完成必须同时满足：

1. 涨跌停参考价不再无条件使用昨日实际收盘；
2. 板块、ST、日期和特殊交易状态规则通过固定样例；
3. 证券级涨跌停事实可追溯且 UNKNOWN 不被误标；
4. 上海 ST 和规则覆盖不足不会伪装成全市场情绪；
5. open/close 特征、未来结果和模型预测物理隔离；
6. 外部历史数据具有字段级可获得性证据；
7. 题材 taxonomy 为双时态且不回填历史；
8. 免费源中断不影响核心日线事实链路；
9. 策略回测前已冻结策略 Spec 和实验注册表；
10. 风险覆盖与选股 alpha 独立消融；
11. 所有正式实验通过统一风险、执行器和 placebo v2；
12. 旧数据、旧执行模式和旧结果未被覆盖。

在以上条件完成前，项目状态保持 `research_data_under_construction`，不得进入实盘评估。
