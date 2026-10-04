# 加仓策略插件与分批持仓账本 v1 开发计划

| 字段 | 内容 |
|---|---|
| 状态 | M0-M8 已实施并自测；M9 隔离资金袖套诊断完成；研究插件默认禁用 |
| 计划版本 | 0.2.0 |
| 创建日期 | 2026-10-04 |
| 对应 Spec | [加仓策略插件与分批持仓账本 v1 设计文档](../specs/position_add_policy_plugin_v1.md) |
| 基线提交 | M0 冻结；不得只记录当前脏工作树的 `HEAD` |
| 当前阶段 | M9 历史诊断完成；等待预登记的新时期前瞻验证 |
| 首期策略 | `NoAddPolicy`、`ProtectedWinnerPolicy` |

## 1. 计划目标

本计划把 Spec 转换为可以顺序实施、自测和审查的工程任务。最终交付：

- 物理持仓、逻辑交易和成交批次三层账本；
- 订单、成交、费用和损益的全链路 lineage；
- T+1、部分退出、卖出预留和公司行为的 lot 级处理；
- 退出优先、ADD 单次有效和确定性状态机；
- 支持加仓的剩余风险、名义上限和成交后去风险；
- 可插拔的 `PositionAddPolicy` 与完整评价审计；
- `NoAddPolicy` 逐笔 golden master；
- `ProtectedWinnerPolicy` 的固定路径 overlay 与完整可执行回放；
- 后续 Rebreakout、TurtleATR 和组合仲裁扩展点；
- Alpha158、VCP 及其组合的批次收益归因和交易可视化。

实施必须按依赖顺序推进。任何里程碑没有通过自测和评审，不得开始依赖它的策略收益实验。

## 2. 工作原则

### 2.1 先账本，后策略

M0 至 M5 只解决领域模型、执行、风险和回归，不根据收益修改加仓参数。`ProtectedWinnerPolicy` 在基础设施通过 golden master 后才实现。

### 2.2 默认行为不变

新架构使用显式开关和 `NoAddPolicy`。关闭加仓时必须逐笔复现冻结基线，不能只比较最终收益。

### 2.3 一次只改变一个研究变量

实验顺序固定为：

```text
旧执行模型
→ 新账本 + NoAdd
→ ProtectedWinner fixed overlay
→ ProtectedWinner executable replay
→ 其他单插件
→ 组合插件
→ 多选股策略组合
```

### 2.4 失败版本保留

所有正式运行使用独立 `experiment_id`、配置哈希、代码标识和数据快照。失败结果只读保留，不以同名目录覆盖。

### 2.5 现实成交约束优先

- 日线收盘产生信号；
- 下一交易日开盘执行；
- ADD 当日未成交即过期；
- A 股 T+1 按 lot 强制执行；
- 跳空、涨跌停、停牌、费用、滑点和公司行为沿用统一执行模型；
- 止损价达到盈亏平衡不能视为无风险。

## 3. 冻结决策

v1 在开发前直接冻结：

| 项目 | v1 决策 |
|---|---|
| 物理订单聚合 | 禁用，一张逻辑订单对应一张物理订单 |
| 跨策略内部净额 | 禁用 |
| 默认 allocation | `GLOBAL`，无独立袖套现金账 |
| 部分退出 | 交易内 FIFO，不允许策略覆盖 |
| ADD 有效期 | `signal_asof` 后下一交易日，未成交即过期 |
| ADD 次数 | 每个逻辑交易最多一次 |
| ADD 风险预算 | 首次成交时按 `ENTRY_EQUITY × 0.125%` 冻结 |
| ADD 名义上限 | `signal_asof` 全局组合净值的 2% |
| 单股总上限 | 沿用组合风险配置的 8% |
| 总股票仓位 | 沿用组合风险配置的 80% |
| 动态止损 | 插件只读，v1 不允许插件修改 |
| 订单数量 | 风险引擎统一计算，插件只给上限 |
| 加仓触发 | 5 日、+1R、距上次成交 +0.5 ATR21、止损价达到含费用 BE |

研究门槛在 M0 实验注册表中冻结：

- 最大回撤相对 NoAdd 基线恶化不超过 1 个百分点；
- matched placebo 重复 1,000 次；
- 历史研究至少报告 30 次 ADD；少于 30 次只作案例研究，不作有效性结论；
- 多插件比较使用 Holm 方法控制多重检验；
- 当前历史已经被反复观察，只能视为研究样本，最终部署判断还需要新的前瞻模拟盘。

## 4. 目标代码与产物

计划新增：

```text
abupy/AlphaBu/
  ABuPositionLedger.py
  ABuPositionAddPolicy.py
  ABuAddProposalArbiter.py
  ABuPositionAddResearch.py

configs/selection/
  position_add_execution_v1.json
  protected_winner_v1.json
  rebreakout_add_v1.json
  turtle_atr_add_v1.json

scripts/
  freeze_position_add_baseline.py
  compare_position_add_golden.py
  backtest_position_add_v1.py
  run_position_add_placebos.py
  visualize_position_add_trades.py

tests/
  test_position_ledger.py
  test_position_lifecycle.py
  test_position_add_risk.py
  test_position_add_policy.py
  test_position_add_golden.py
  test_position_add_research.py
```

计划修改：

```text
abupy/AlphaBu/ABuTradeIntent.py
abupy/AlphaBu/ABuPortfolioExecutor.py
abupy/AlphaBu/ABuPortfolioRisk.py
abupy/AlphaBu/ABuVCPStrategy.py
abupy/AlphaBu/ABuAlpha158Lite.py
abupy/AlphaBu/ABuTradeVisualization.py
```

正式产物：

```text
baseline_manifest.json
schema_mapping.json
golden_comparison.json
policy_evaluations.csv
add_proposals.csv
logical_orders.csv
physical_fills.csv
fill_allocations.csv
position_lots.csv
lot_dispositions.csv
logical_trades.csv
physical_positions_daily.csv
risk_decisions.csv
trade_attribution.csv
strategy_summary.csv
```

## 5. 里程碑总览

| 里程碑 | 内容 | 依赖 | 主要门禁 |
|---|---|---|---|
| M0 | 冻结基线、比较契约和实验注册 | 无 | 基线身份和输入哈希完整 |
| M1 | Lineage 与三层持仓 schema | M0 | schema、迁移和 ID 稳定 |
| M2 | FillAllocation、LotDisposition 和守恒账本 | M1 | 部分退出后数量、费用、现金守恒 |
| M3 | 生命周期、T+1、退出冲突和公司行为 | M2 | 状态转移完整、EXIT 不遗留 ADD |
| M4 | ADD 风险余量、结算和去风险 | M3 | 不重复使用额度、trade-scoped 去风险 |
| M5 | 插件接口、NoAdd 和 golden master | M4 | Alpha158/VCP 逐笔复现 |
| M6 | ProtectedWinner 与双回测口径 | M5 | 参数冻结、overlay 与实盘口径分离 |
| M7 | Rebreakout、TurtleATR 和组合仲裁 | M6 | 单插件先验收，组合无重复订单 |
| M8 | 多策略组合、placebo、归因和可视化 | M7 | 同股多交易隔离、完整研究报告 |
| M9 | TurtleATR 隔离资金袖套与市场趋势门控 | M8 | 基础路径不受 ADD 现金占用影响；保留失败版本 |

关键路径：

```text
M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8
```

每个里程碑结束后生成：

```text
docs/reviews/position_add_v1_m<N>_review.md
```

审查记录包含实现内容、测试命令、结果、差异、遗留风险和是否允许进入下一阶段。

## 6. M0：冻结基线与比较契约

### 6.1 任务

#### M0-T1 冻结代码状态

- 记录 `git_commit`；
- 工作树非空时保存完整补丁并计算 `working_tree_patch_sha256`；
- 记录未跟踪研究代码的文件哈希；
- 不把 `HEAD` 错误描述成完整基线；
- 保存 Python、NumPy、pandas 和关键依赖版本。

#### M0-T2 冻结数据与配置

- 保存行情、PIT 股票池、行业、ST、公司行为和预测文件哈希；
- 分别冻结 Alpha158 与 VCP 的运行日期范围；
- 保存风险配置、执行配置、涨跌停模型版本；
- 保存动态止损同步开关、滑点、费用和随机种子；
- 登记当前基线回测目录和所有核心文件哈希。

#### M0-T3 实现基线冻结工具

新增 `freeze_position_add_baseline.py`：

- 对输入、配置和输出生成稳定排序清单；
- 生成 `baseline_manifest.json`；
- 计算总 `baseline_id`；
- 拒绝覆盖已有基线；
- 验证产物可读、日期有序且主键不重复。

#### M0-T4 实现规范化比较器

新增 `compare_position_add_golden.py`：

- 加载 Spec 中的新旧 schema 映射；
- 按冻结业务键排序；
- 精确比较离散字段；
- 按 Spec 容差比较浮点字段；
- 忽略旧 schema 不存在的 lineage 字段；
- 校验新 ID 在重复运行中的稳定性；
- 输出逐表差异和首个不一致位置。

#### M0-T5 冻结研究注册

- 注册 A0、A1、V0、V1 实验；
- 固定 ProtectedWinner 参数；
- 固定 1 个百分点回撤容忍；
- 固定 1,000 次 placebo；
- 固定最少 30 次 ADD 的解释门槛；
- 明确历史样本不是新的最终留出期。

### 6.2 自测

- 同一输入两次生成相同 `baseline_id`；
- 修改任一配置或输入文件一个字节后哈希变化；
- 工作树补丁变化能够被识别；
- 文件遍历顺序不影响哈希；
- 比较器能定位数量、费用、原因码和曲线差异；
- 新增 lineage 字段不会制造伪差异；
- 超出容差的单点差异使比较失败。

### 6.3 完成条件

- Alpha158 与 VCP 基线身份完整；
- 每个运行命令和输入可复现；
- golden master 比较器有单元测试；
- M0 review 明确允许进入 schema 开发。

## 7. M1：Lineage 与三层持仓 schema

### 7.1 任务

#### M1-T1 定义领域对象

在 `ABuPositionLedger.py` 实现不可变记录：

- `PhysicalPosition`；
- `LogicalTrade`；
- `PositionLot`；
- `FillAllocation`；
- `LotDisposition`；
- `SellReservation`；
- 状态与 `position_effect` 枚举。

#### M1-T2 扩展交易记录

为 `TradeIntent`、`ApprovedOrder` 和 `Fill` 增加正式字段：

- `trade_id` 或 `target_trade_id`；
- `allocation_id`；
- `position_effect`；
- policy、proposal 和逻辑/物理订单 lineage；
- schema version。

不得仅依赖 `metadata` 传递关键归属。

#### M1-T3 确定性 ID

- 实现评价、提案、逻辑订单、物理订单、成交分配、lot 和 disposition ID；
- 分层实现 evaluation、proposal 和 logical ADD order 唯一键；
- 重复运行生成相同业务 ID；
- `logical_add_order_key` 不含 `policy_id`。

#### M1-T4 旧结构迁移适配器

- 一个旧 `Position` 映射为一个物理持仓、一个逻辑交易和一个 OPEN lot；
- 旧订单映射为一对一逻辑/物理订单；
- 旧成交映射为一个 physical fill 和一个 fill allocation；
- 保存旧 ID 到新业务键的映射表。

### 7.2 自测

- dataclass/schema 字段完整且拒绝未知字段；
- `buy+OPEN`、`buy+INCREASE`、`sell+REDUCE`、`sell+CLOSE` 合法；
- 非法 side/effect 组合失败；
- 相同输入 ID 稳定；
- 不同插件同一交易同一时点只能映射到一个 logical ADD order key；
- 旧持仓迁移后数量和账面成本不变；
- `remaining_book_cost_cash` 和价格字段语义不混用。

### 7.3 完成条件

- 所有核心对象可序列化和回读；
- lineage 能从逻辑交易追溯到物理成交；
- 迁移适配器测试通过；
- 尚未改变现有回测经济结果。

## 8. M2：分批账本、成交分配与部分退出

### 8.1 任务

#### M2-T1 买入成交记账

- 每笔 OPEN/INCREASE 正数量成交创建 `FillAllocation`；
- 每条买入分配创建一个 `PositionLot`；
- 使用实际成交价和买入费用形成账面成本；
- 滑点作为成交质量归因，不重复进入现金成本。

#### M2-T2 卖出成交记账

- 在目标 `trade_id` 内按 FIFO 选择可卖 lots；
- 创建卖出 `FillAllocation`；
- 为每个被消耗 lot 创建 `LotDisposition`；
- 按数量分配卖出费用并稳定处理尾差；
- 计算处置成本和已实现收益；
- 更新 lot、逻辑交易和物理持仓余额。

#### M2-T3 守恒断言

每个成交后自动检查：

- 物理成交数量等于逻辑分配数量；
- 卖出分配数量等于 dispositions 数量；
- 物理费用等于逻辑分配费用；
- 物理持仓等于所有活动 lot 数量；
- 物理现金变化只发生一次；
- 已实现收益与现金、成本基础可复算。

#### M2-T4 一对一物理执行

- 强制每张逻辑订单生成独立物理订单；
- 禁止物理聚合和内部净额；
- 每张物理订单独立计算最低佣金；
- 增加未来聚合版本的显式拒绝开关。

### 8.2 自测

- OPEN 买入创建正确 lot 和成本；
- INCREASE 买入创建独立 lot；
- 单 lot 全部卖出；
- 单 lot 部分卖出；
- 多 lot FIFO 卖出跨越批次；
- 卖出费用尾差确定性分配；
- 实际成交价已含滑点，不重复扣除；
- 物理和逻辑现金、数量、费用守恒；
- 两张同股逻辑订单分别收费和成交。

### 8.3 完成条件

- `FillAllocation/LotDisposition` schema 落地；
- 部分退出后的收益可按 trade 和 lot 复算；
- 任一守恒失败都会终止回测并给出记录 ID。

## 9. M3：生命周期、退出冲突、T+1 与公司行为

### 9.1 任务

#### M3-T1 实现状态机

只保留：

```text
PENDING_OPEN / ACTIVE / EXIT_REQUESTED /
EXIT_PENDING / CLOSED / CANCELLED
```

- 按 Spec 状态表实现事件处理；
- ADD pending 只由订单集合表达；
- 非法转换失败关闭；
- 状态、订单和预留事务式更新。

#### M3-T2 退出优先

- 收盘先更新退出和动态止损，再评价 ADD；
- EXIT 请求原子取消 ADD 提案和订单；
- 释放现金和风险预留；
- 开盘成交前再次确认目标 trade 为 ACTIVE；
- ADD 不得重新创建已经关闭的逻辑交易。

#### M3-T3 T+1 与卖出预留

- 每个 lot 保存 `sellable_date`；
- 创建卖单时按 lot 建立 `SellReservation`；
- 延期卖单保持预留；
- 取消、成交和到期正确释放；
- 不可卖部分保持 `EXIT_PENDING`。

#### M3-T4 公司行为

- 现金分红按有权 lots 分配；
- 送转、拆并股按 lot 调整数量、成本和原始价止损；
- 余股按小数余数和 `lot_id` 确定性分配；
- 新增股份按事件可交易日期设置 `sellable_date`；
- 日期或来源未知时失败关闭并记录原因；
- 退市、收购和换股保持 lineage。

### 9.2 自测

- 覆盖状态表所有合法转换；
- 每个非法转换均被拒绝；
- EXIT 与 ADD 同日时只保留 EXIT；
- 卖单延期期间不能 ADD；
- ADD 成交前交易关闭时订单取消；
- 当日买入 lot 不可卖，下一交易日可卖；
- 部分可卖时只卖可卖部分；
- 公司行为后物理和逻辑数量守恒；
- 公司行为与延期卖单相邻时预留正确调整。

### 9.3 完成条件

- 不存在退出后 ADD 重新开仓路径；
- T+1 由 lot 明确表达；
- 公司行为不会破坏数量、成本和 lineage；
- 状态与订单审计可完整重放。

## 10. M4：加仓风险、结算与去风险

### 10.1 任务

#### M4-T1 风险余量

扩展风险引擎：

- 单股额度扣除已有持仓和 pending 买单；
- 逻辑交易额度扣除已成交和预留 ADD 风险；
- 2% 名义上限转成 `quantity_notional_cap`；
- 保留总仓位、开放风险、行业、同日、容量、现金和压力限制；
- 每个中间数量写入决策记录。

#### M4-T2 风险预留结算

- 批准时增加 `reserved_add_risk_cash`；
- 成交时释放计划预留并登记实际冻结风险；
- 拒绝、取消、过期时完整释放；
- `add_count` 只在正数量成交后增加；
- 移动止损和卖出不追溯释放历史冻结 ADD 风险。

#### M4-T3 多逻辑交易开放风险

- 每个 trade 使用自己的当前可执行止损；
- 再按 symbol、industry、portfolio 聚合；
- 一个策略止损变化不改变另一个策略的逻辑止损；
- 压力损失按物理敞口计算且避免重复计数。

#### M4-T4 成交后去风险

- 先取消未成交新增风险；
- 优先本次开盘新增 ADD，再处理其他 ADD，再处理 OPEN lots；
- 同层按边际风险贡献、成交时间和稳定 ID 排序；
- 生成 trade-scoped REDUCE/CLOSE；
- 当日 ADD 因 T+1 不可卖时生成最早可执行计划并持续披露风险。

### 10.2 自测

- 已有单股仓位正确减少 ADD headroom；
- pending 买单不能重复使用单股额度；
- 多次评价不能重复预留 ADD 风险；
- 2% 名义上限基于 signal_asof 全局净值；
- 成交、拒绝、取消和过期后的风险结算正确；
- 同股两个 trade 的开放风险分别计算后聚合；
- 去风险只减少目标 trade/lots；
- T+1 阻止当日卖出并保留压力暴露。

### 10.3 完成条件

- ADD 不可绕过任何风险上限；
- 所有风险预留可以从事件重放复算；
- 成交后去风险顺序确定且不按 symbol 清空全部策略仓位。

## 11. M5：插件框架、NoAdd 与 Golden master

### 11.1 任务

#### M5-T1 插件协议

实现：

- 不可变 `PositionAddContext`；
- `PolicyEvaluation`；
- `AddProposal`；
- `PositionAddPolicy` 协议；
- 必填字段与缺失数据失败关闭；
- 配置、数据版本和输入快照审计。

#### M5-T2 ADD 有效期与幂等

- `valid_session` 固定为下一交易日；
- 未成交自动过期并释放预留；
- 新日期重新评价，不复用旧订单；
- 实现三层唯一键；
- 同一 trade/date/sequence 最多一张逻辑 ADD 订单。

#### M5-T3 `NoAddPolicy`

- 每次返回未触发评价；
- 不生成提案、订单或预留；
- 评价记录原因码与版本；
- 默认配置加载该插件。

#### M5-T4 Golden master 回归

分别运行 Alpha158 和 VCP 的冻结模式：

- 意图；
- 风险决策；
- 订单；
- 成交；
- 费用；
- 现金与每日曲线；
- 持仓事件；
- 退出日期与原因；
- 压力结果。

### 11.2 自测

- 修改未来数据不改变当日评价；
- NoAdd 永不产生提案；
- 相同输入评价和业务 ID 稳定；
- 不同插件不能生成重复 logical ADD order；
- ADD 次日未成交自动过期；
- Alpha158 与 VCP 每张 golden 表通过比较器；
- 新账本守恒断言全部通过。

### 11.3 完成条件

- `NoAddPolicy` 逐笔复现全部冻结基线；
- 任一不可解释差异为阶段失败；
- golden 未通过前禁止实现或回测 ProtectedWinner。

## 12. M6：ProtectedWinner 与双回测口径

### 12.1 任务

#### M6-T1 实现冻结公式

实现 `ProtectedWinnerPolicy`：

- ACTIVE 且无退出、无 pending ADD；
- 持仓和距上次买入至少 5 个交易日；
- `add_count == 0`；
- 原选股策略仍为 HOLD；
- 至少 +1R；
- 距上次成交至少 +0.5 ATR21；
- 当前止损价达到含费用盈亏平衡；
- 风险 0.125%，名义上限 2%；
- 审计码使用 `STOP_LEVEL_AT_BREAKEVEN`。

#### M6-T2 含费用盈亏平衡

- 使用活动 lots 的实际账面成本；
- 包含预计卖出佣金、过户费、印花税和滑点；
- 使用原始价空间；
- 保存每项输入和公式版本；
- 不把该条件解释为真实利润保证。

#### M6-T3 固定路径 overlay

- 冻结基础入场、退出、动态止损和费用路径；
- 虚拟 ADD 使用固定 overlay 本金或风险预算；
- 独立计算费用、滑点、MFE、MAE 和增量 R；
- 不修改普通账户净值；
- 不改变基础交易退出日期。

#### M6-T4 完整可执行回放

- ADD 真实占用现金和风险；
- 后续基础入场可以因资源占用被拒绝；
- 报告收益、回撤、仓位、压力、换手和拒单；
- 分开基础 lots 与 ADD lots 收益。

### 12.2 自测

- 每个触发条件独立边界测试；
- 缺失 ATR、止损、成本或复权因子时失败关闭；
- 原始价与复权价映射正确；
- +1R 和 0.5 ATR 窗口不读取未来数据；
- overlay 不改变基础现金、订单和退出；
- executable replay 正确改变现金和后续风险；
- ADD lot 的 T+1、费用和公司行为正确。

### 12.3 完成条件

- A1 与 V1 两类回放均生成完整审计产物；
- overlay 与 executable 指标不混用；
- ADD 样本不足 30 时报告样本不足，不作有效性结论；
- 参数不因已观察结果修改。

## 13. M7：其他插件与组合仲裁

### 13.1 任务

#### M7-T1 `RebreakoutPolicy`

- 使用 `t-20...t-1` 调整价高点；
- 当前日不进入历史窗口；
- 规则和数据快照独立版本化；
- 首版不增加观察结果驱动的成交额参数。

#### M7-T2 `TurtleAtrPolicy`

- 相对上次买入价格上涨 0.5 ATR21；
- 数量仍由统一风险引擎计算；
- 首版最多一次 ADD；
- 不采用原始海龟系统的高风险比例。

#### M7-T3 仲裁器

- 实现 ALL_OF、ANY_OF、PRIORITY；
- 组合只处理同一 trade 和 signal_asof；
- 风险、数量、价格取更严格约束；
- 所有被压制提案保留评价记录；
- logical ADD order key 去除 policy 维度，防止重复订单。

#### M7-T4 单插件先验收

- 先运行 Rebreakout 和 TurtleATR 单插件；
- 检查触发覆盖、费用和增量收益；
- 单插件生命周期正确后，才运行 `ProtectedWinner ALL_OF Rebreakout`；
- 不自动遍历全部插件组合。

### 13.2 自测

- Rebreakout 窗口排除当前日；
- TurtleATR 使用信号日已知 ATR；
- ALL_OF 保存未通过成员原因；
- ANY_OF 和 PRIORITY 不生成多张 ADD；
- 合并后的价格、数量和风险约束为成员交集；
- 被压制和取消提案可审计；
- 多插件配置顺序不会意外改变 ALL_OF 结果。

### 13.3 完成条件

- 每个单插件都能独立启用和关闭；
- 组合仲裁不破坏幂等和风险预留；
- 所有实验在注册表中预先登记。

## 14. M8：多策略组合、placebo、归因与可视化

### 14.1 任务

#### M8-T1 Alpha158 + VCP 组合

- 同一股票允许两个独立 `LogicalTrade`；
- 各自拥有退出状态、动态止损和 lots；
- 一个策略退出只卖自己的 lots；
- 物理持仓、现金、行业和压力风险统一聚合；
- `GLOBAL` allocation 下共享现金和风险上限。

#### M8-T2 匹配随机加仓

- 固定加仓日期、次数和风险规模；
- 匹配持仓年龄、浮盈 R、流动性、行业和市场状态；
- 只使用当时可见字段；
- 运行 1,000 次；
- 输出收益分位数、置信区间和最大回撤分布。

#### M8-T3 绩效归因

- 组合、选股策略、逻辑交易、插件和 lot 五层报告；
- 基础 lots 与 ADD lots 分离；
- 报告 ADD 占用资源造成的基础入场拒绝；
- 报告年份、行业、股票和市场状态集中度；
- Holm 方法处理多插件比较。

#### M8-T4 可视化

- 基础买入、ADD、部分退出和完全退出使用不同标记；
- 显示插件触发原因和风险审批；
- 按 trade、选股策略和加仓插件筛选；
- 同时提供物理持仓聚合视图；
- 展示动态止损、公司行为和延期退出。

#### M8-T5 最终验证报告

- 汇总 A0/A1、V0/V1 和组合策略；
- 同时呈现 fixed overlay 与 executable replay；
- 给出收益、回撤、仓位、压力、费用和统计证据；
- 明确失败、样本不足和前瞻模拟要求；
- 不因提高仓位而自动判定成功。

### 14.2 自测

- Alpha158 退出不影响 VCP lots；
- VCP 退出不影响 Alpha158 lots；
- 同股不同止损的开放风险聚合正确；
- placebo 不读取未来退出日；
- 重复随机种子生成相同结果；
- 报告总收益等于各归属层汇总；
- 可视化交易数量与账本一致；
- 每个图形标记可追溯到 fill/disposition。

### 14.3 完成条件

- 组合策略账本与物理账户完全守恒；
- 研究报告能区分选股贡献和加仓贡献；
- 结果具备进入或拒绝前瞻模拟盘的明确结论。

## 15. 全程测试策略

每个里程碑至少执行：

```bash
python3 -m pytest <本里程碑相关测试> -q
python3 -m compileall <本里程碑修改的 Python 文件>
git diff --check
```

阶段末再运行：

```bash
python3 -m pytest tests -q
```

测试扩展原则：

- 优先验证状态、守恒、时序和无前视；
- 不为简单字段赋值编写没有行为价值的测试；
- 修复缺陷时先补能复现缺陷的测试；
- golden master 不允许用更新基线掩盖差异；
- 收益差异不是基础设施测试通过的依据。

## 16. 阶段停止条件

出现以下任一情况时停止后续里程碑并修复：

- 物理与逻辑数量、费用或现金不守恒；
- 一个策略退出影响另一策略 lots；
- ADD 订单可在目标交易关闭后成交；
- ADD 订单跨日自动延续；
- 风险预留取消后未释放；
- 单股、交易或组合额度可被重复使用；
- T+1 被绕过；
- `NoAddPolicy` 无法解释地偏离冻结基线；
- 插件读取未来数据；
- fixed overlay 被并入普通账户总收益；
- 正式实验参数在观察结果后被修改。

## 17. 交付与提交边界

每个里程碑完成后：

1. 保存测试输出摘要；
2. 生成对应 review 文档；
3. 更新计划状态和完成证据；
4. 检查未跟踪产物和大文件；
5. 仅在用户明确要求时提交或推送代码；
6. 任何提交都包含明确的配置和文档版本，不包含本地大体积回测产物。

最终代码完成不等于策略有效。只有 M8 研究门槛通过并完成新的前瞻模拟，才允许讨论扩大风险预算或长期模拟盘部署。

## 18. M9：隔离资金袖套诊断

M9 用于区分 TurtleATR 的信号收益与共享账户资金挤占，不修改
`turtle_atr_add_v1` 的 0.5 ATR、风险预算、名义上限或最大加仓次数。

冻结的诊断结构为：

```text
100 万总资金
→ 90 万 Alpha158 基础账户
→ 10 万 ADD 独立现金袖套
→ 对照组将 10 万保持现金
```

基础策略以 shadow 方式产生 ADD 提案，袖套按相同的次日开盘、费用、
滑点、涨停过滤、整手和公司行为规则独立记账，退出日使用基础逻辑交易的
实际可成交退出日。袖套不得改变基础订单、持仓或风险审批路径。

M9-T1 实现现金隔离回放并验证现金守恒；M9-T2 运行原始 TurtleATR；
M9-T3 增加独立版本的 PIT 市场趋势门控，仅在基准收盘高于当日 MA200 时
允许加仓。M9-T3 是观察 M9-T2 后形成的风险诊断，必须标记为 post hoc，
历史改善不能用于统计准入。

结果记录在
`docs/reviews/position_add_isolated_sleeve_m9_review_20261004.md`。市场趋势门控
通过历史收益、回撤和样本数工程门槛，但既有严格 placebo 覆盖不足，且该
门控属于事后诊断，因此保持 `research_only`，默认策略仍为 `NoAddPolicy`。
