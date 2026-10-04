# 加仓策略插件与分批持仓账本 v1 设计文档

| 字段 | 内容 |
|---|---|
| 状态 | Draft；CR 修订后，实施前冻结 |
| 设计版本 | 0.2.0 |
| 创建日期 | 2026-10-04 |
| 适用市场 | 沪深 A 股日线研究回测 |
| 上位规范 | [A 股选股研究与组合风险引擎 v2](selection_research_engine_v2.md) |
| 关联规范 | [动态止损与组合风险同步 v1](dynamic_stop_risk_sync_v1.md) |
| 配套计划 | [加仓策略插件与分批持仓账本 v1 开发计划](../plans/position_add_policy_plugin_v1_development_plan.md) |
| 首期候选 | `NoAddPolicy`、`ProtectedWinnerPolicy` |
| 实施原则 | 先冻结账本、归属、退出和风险语义，再实现加仓公式 |

## 1. 背景与设计结论

现有系统已经把选股意图、风险审批、次日开盘成交、资金账、动态止损和压力损失分开，但持仓模型仍以 `symbol` 为唯一主键。一只股票只能保存一个策略来源、一个入场价格和一个当前止损，执行器也会拒绝对已有持仓再次买入。

加仓研究需要同时支持：

- Alpha158、VCP 和后续选股策略使用同一套加仓能力；
- 同一选股策略在不同加仓策略下做消融；
- 多个选股策略组成组合时，各自拥有独立退出和绩效归属；
- 物理账户按股票聚合持仓，研究账本按逻辑交易和成交批次拆分；
- 所有加仓提案经过同一套现金、风险、压力和成交审批。

因此，本设计采用以下分层：

```text
Selection Strategy
→ EntryIntent / ExitIntent
→ LogicalTrade
→ PositionAddPolicy
→ PolicyEvaluation / AddProposal
→ AddProposalArbiter
→ PortfolioRiskEngine
→ ApprovedOrder
→ Physical Execution
→ FillAllocation
→ PositionLot / PhysicalPosition
```

选股策略回答“持有什么”；加仓插件回答“已经持有后，是否建议增加”；风险引擎回答“允许增加多少”；执行器回答“能否真实成交”。任何加仓插件不得直接修改现金、持仓、止损或订单。

## 2. 目标

本版本必须定义并为实施提供确定语义：

1. `PhysicalPosition`、`LogicalTrade` 和 `PositionLot` 三层持仓模型；
2. 从意图到成交批次的完整 lineage；
3. `OPEN/INCREASE/REDUCE/CLOSE` 对持仓的影响类型；
4. 退出订单与加仓订单的冲突规则；
5. T+1、部分退出、延期卖出、公司行为和费用分摊；
6. 加仓插件、组合插件和提案仲裁接口；
7. 既有仓位、待成交订单和累计加仓预算下的风险余量；
8. 原始价、复权价、初始 R 和当前止损的价格空间；
9. `NoAddPolicy` 的逐笔回归标准；
10. 固定基础路径 overlay 与完整可执行回放的不同记账口径；
11. 单选股策略和组合选股策略下的绩效归因；
12. 可审计、幂等、无前视的测试与验收要求。

## 3. 非目标

首期不包含：

- 实盘下单；
- 分钟或逐笔撮合；
- 做空、融资或融券；
- 亏损摊平、马丁格尔或 DCA；
- 加仓插件修改动态止损公式；
- 自动搜索加仓参数或自动选择最优插件组合；
- 2% 或 4% 单笔风险；
- 任意数量的重复加仓；
- 北京证券交易所股票；
- 把固定路径 overlay 的虚拟收益解释为普通账户收益；
- 把仓位提高本身解释为策略有效。

首期 `PositionAddPolicy` 只允许提出 `INCREASE`。减仓、止损调整和再平衡若进入后续版本，使用独立接口或提升为更广义的 `PositionAdjustmentPolicy`。

## 4. 强制不变量

### 4.1 归属不变量

- 每个逻辑交易有唯一且稳定的 `trade_id`；
- 每个成交批次有唯一且稳定的 `lot_id`；
- 每个资本归属单元有 `allocation_id`，默认值为 `GLOBAL`；
- `ApprovedOrder`、`Fill` 和 `FillAllocation` 必须显式携带 lineage，不能只写入自由格式 `metadata`；
- 一个 `PositionLot` 只属于一个 `LogicalTrade`；
- 一个 `LogicalTrade` 只属于一个选股策略版本和一个 `allocation_id`；
- 一个 `PhysicalPosition` 可以聚合多个逻辑交易，但不能覆盖其独立退出状态。

### 4.2 风险不变量

- 加仓不能重新获得一份不受累计限制的初始单笔风险预算；
- 单股上限必须扣除已有物理持仓和待成交买单；
- 组合、行业、同日新增、成交容量、现金和压力损失限制继续生效；
- 加仓插件只提出风险预算上限和数量上限，最终数量由风险引擎确定；
- 动态止损达到盈亏平衡只表示止损价位条件成立，不表示利润已经锁定；
- 跳空、停牌和连续跌停压力不得因为止损达到盈亏平衡而归零；
- `initial_r_cash_frozen` 不随移动止损、加仓或公司行为被追溯改写。

### 4.3 时序不变量

- 插件在 `signal_asof` 只能读取当时已经可见的数据；
- 当日收盘信号只能生成下一交易日及以后有效的订单；
- 当日退出判断优先于当日加仓判断；
- 退出请求原子地取消所属逻辑交易的加仓提案、未成交订单和预留；
- ADD 成交前必须重新确认目标 `trade_id` 仍为可加仓状态；
- 买入批次在其 `sellable_date` 之前不得用于卖出；
- 加仓不得重置基础交易的入场日期、停滞退出时钟或历史初始 R。

### 4.4 审计不变量

- 插件每次评估都返回 `PolicyEvaluation`，即使没有触发；
- 组合仲裁必须保存通过、拒绝、合并、压制和取消的全部原因；
- 同一交易、信号时点和加仓序号重复运行不得产生第二张订单；
- 配置、代码、数据和基础交易路径必须有冻结版本或哈希；
- 未知数据失败关闭，不得填成对触发加仓有利的值。

## 5. 当前系统差距

| 当前行为 | 影响 | 目标修正 |
|---|---|---|
| `positions[symbol]` 只有一个 `Position` | 多策略来源相互覆盖 | 三层持仓模型 |
| 已持仓股票再次买入被判为 `DUPLICATE_POSITION` | 无法表达 ADD | 仅允许带有效 `trade_id` 的 `INCREASE` |
| 退出状态以 `symbol` 保存 | 一个策略退出会影响其他策略 | 每个 `LogicalTrade` 独立退出状态 |
| `ApprovedOrder/Fill` 无 `trade_id/lot_id/position_effect` | 成交与收益无法归属 | 全链路显式 lineage |
| 单股风险上限未扣已有仓位 | ADD 可重复使用单股额度 | 使用 symbol headroom |
| 一个 `Position` 只有一个止损 | 多逻辑交易风险无法拆分 | 按逻辑交易聚合开放风险 |
| 卖单先于买单，但 ADD 无目标交易复核 | 退出后可能重新开仓 | 退出优先和成交前硬复核 |
| 公司行为调整汇总持仓 | 分批成本和余股无法确定 | 按 lot 确定性分配 |

## 6. 目标架构

```mermaid
flowchart LR
    A[Selection Strategy] --> B[Entry / Exit Intent]
    B --> C[Logical Trade Registry]
    C --> D[Position Add Policies]
    D --> E[Policy Evaluations]
    E --> F[Proposal Arbiter]
    F --> G[Portfolio Risk Engine]
    G --> H[Logical Approved Orders]
    H --> I[Physical Order Batcher]
    I --> J[Execution and Fill]
    J --> K[Fill Allocation]
    K --> L[Position Lots]
    L --> M[Physical Position]
    L --> C
    M --> G
    C --> G
```

### 6.1 责任边界

| 模块 | 负责 | 不负责 |
|---|---|---|
| Selection Strategy | 初始候选、首次入场、所属逻辑交易退出 | ADD 数量、现金和组合风险 |
| PositionAddPolicy | 根据已有交易状态提出加仓建议 | 直接建仓、改止损、改账本 |
| Proposal Arbiter | 合并同一交易同一时点的策略评价 | 放宽插件约束或风险限制 |
| PortfolioRiskEngine | 计算所有剩余额度和最终数量 | 选择股票或创造交易信号 |
| Executor | 订单有效性、成交、费用、T+1和公司行为 | 修改信号或风险参数 |
| Attribution | 将物理成交与损益分配到逻辑交易和 lots | 改变真实现金账 |

## 7. 核心领域模型

### 7.1 `PhysicalPosition`

表示账户在交易所层面对一个证券的真实净持仓。

```text
PhysicalPosition
├── symbol
├── total_quantity
├── sellable_quantity
├── reserved_sell_quantity
├── remaining_book_cost_cash
├── market_value_cash
├── logical_trade_ids[]
├── active_physical_order_ids[]
└── lifecycle_version
```

强制关系：

```text
total_quantity
= sum(active lots.quantity_remaining)

sellable_quantity
= sum(active lots.quantity_remaining
       where lot.sellable_date <= current_date)
   - reserved_sell_quantity
```

`PhysicalPosition` 不保存唯一策略 ID 或唯一策略止损。

其中：

```text
remaining_book_cost_cash
= sum(active lots.remaining_book_cost_cash)

market_value_cash
= mark_price_raw × total_quantity
```

### 7.2 `LogicalTrade`

表示由某个选股策略创建并独立管理退出的交易实例。

```text
LogicalTrade
├── trade_id
├── allocation_id
├── symbol
├── selection_strategy_id
├── selection_strategy_version
├── entry_intent_id
├── status
├── opened_at
├── closed_at
├── lot_ids[]
├── pending_add_order_ids[]
├── pending_exit_order_ids[]
├── add_count
├── max_add_count
├── initial_entry_price_adjusted
├── initial_entry_price_raw
├── initial_stop_adjusted
├── current_stop_adjusted
├── current_stop_raw
├── initial_r_per_share_adjusted
├── initial_r_cash_frozen
├── add_risk_budget_cash_frozen
├── filled_add_risk_cash_frozen
├── reserved_add_risk_cash
├── highest_close_adjusted
├── entry_session_index
├── last_buy_fill_session_index
└── last_add_session_index
```

状态枚举：

```text
PENDING_OPEN
ACTIVE
EXIT_REQUESTED
EXIT_PENDING
CLOSED
CANCELLED
```

ADD 是否待成交由 `pending_add_order_ids` 和订单状态表达，不进入 `LogicalTrade.status`。逻辑交易状态只描述从创建到关闭的生命周期。

### 7.3 `PositionLot`

每次真实买入成交创建一个 lot。

```text
PositionLot
├── lot_id
├── trade_id
├── allocation_id
├── symbol
├── position_effect        # OPEN / INCREASE
├── source_policy_id
├── source_policy_version
├── order_id
├── fill_id
├── fill_date
├── sellable_date
├── original_quantity
├── quantity_remaining
├── fill_price_raw
├── allocated_commission_cash
├── allocated_transfer_fee_cash
├── allocated_stamp_tax_cash
├── allocated_slippage_cash
├── remaining_book_cost_cash
├── stop_raw_at_fill
├── risk_per_share_raw_at_fill
├── risk_cash_frozen
├── adjustment_factor_at_fill
└── status
```

首期同一 `LogicalTrade` 的 lots 共用逻辑交易当前止损。lot 保存成交时止损和冻结风险用于归因，组合开放风险使用逻辑交易当前可执行止损。

### 7.4 `FillAllocation`

`FillAllocation` 表示一笔物理成交向一个逻辑订单的数量、现金和费用分配。即使 v1 是一张逻辑订单对应一张物理订单，也必须生成一条分配记录。

```text
FillAllocation
├── fill_allocation_id
├── physical_fill_id
├── physical_order_id
├── logical_order_id
├── trade_id
├── allocation_id
├── symbol
├── side
├── position_effect
├── allocated_quantity
├── allocated_fill_price_raw
├── allocated_gross_cash
├── allocated_commission_cash
├── allocated_transfer_fee_cash
├── allocated_stamp_tax_cash
├── allocated_slippage_cash
├── created_lot_id_optional
└── created_at
```

`fill_allocation_id` 是成交分配记录 ID；`allocation_id` 是资本与策略归属 ID，两者不得混用。

### 7.5 `LotDisposition`

`LotDisposition` 表示卖出成交对某个买入 lot 的实际消耗，是已实现收益的唯一事实来源。

```text
LotDisposition
├── disposition_id
├── physical_fill_id
├── fill_allocation_id
├── logical_order_id
├── trade_id
├── allocation_id
├── lot_id
├── symbol
├── disposed_quantity
├── allocated_gross_proceeds_cash
├── allocated_sell_commission_cash
├── allocated_sell_transfer_fee_cash
├── allocated_stamp_tax_cash
├── allocated_sell_slippage_cash
├── disposed_book_cost_cash
├── realized_pnl_cash
├── exit_reason
└── fill_date
```

部分卖出的成本基础按该 lot 的剩余账面成本比例计算：

```text
disposed_book_cost_cash
= lot.remaining_book_cost_cash_before
   × disposed_quantity / lot.quantity_remaining_before

realized_pnl_cash
= allocated_gross_proceeds_cash
   - allocated_sell_commission_cash
   - allocated_sell_transfer_fee_cash
   - allocated_stamp_tax_cash
   - disposed_book_cost_cash
```

`allocated_gross_proceeds_cash` 使用实际 `fill_price_raw`，因此已经包含滑点影响；`allocated_sell_slippage_cash` 只做执行质量归因，不得再次从现金或已实现收益中扣除。买入 lot 的账面成本使用实际成交金额加买入费用，滑点同样已包含在成交价中。

### 7.6 数量、费用与现金守恒

```text
physical_fill.quantity
= sum(fill_allocations.allocated_quantity)

physical_fill.gross_cash
= sum(fill_allocations.allocated_gross_cash)

physical_fill.commission
= sum(fill_allocations.allocated_commission_cash)

physical_fill.transfer_fee
= sum(fill_allocations.allocated_transfer_fee_cash)

physical_fill.stamp_tax
= sum(fill_allocations.allocated_stamp_tax_cash)

physical_fill.slippage_cost
= sum(fill_allocations.allocated_slippage_cash)

sell_fill_allocation.allocated_quantity
= sum(lot_dispositions.disposed_quantity)

lot.quantity_remaining
= lot.original_quantity
   + corporate_action_quantity_delta
   - sum(lot_dispositions.disposed_quantity)

buy_physical_cash_delta
= -(physical_fill.gross_cash
    + physical_fill.commission
    + physical_fill.transfer_fee
    + physical_fill.stamp_tax)

sell_physical_cash_delta
= physical_fill.gross_cash
   - physical_fill.commission
   - physical_fill.transfer_fee
   - physical_fill.stamp_tax
```

物理现金变化必须等于全部物理成交净现金流与公司行为现金流之和；逻辑分配只做归属，不得再次改变物理现金。

### 7.7 `position_effect`

`side` 与持仓影响必须分开：

| side | position_effect | 含义 |
|---|---|---|
| buy | OPEN | 创建新的逻辑交易 |
| buy | INCREASE | 增加已有逻辑交易 |
| sell | REDUCE | 部分减少指定逻辑交易 |
| sell | CLOSE | 关闭指定逻辑交易 |

首期加仓插件只能生成 `buy + INCREASE`。

### 7.8 Lineage 字段

以下字段必须成为正式 schema：

```text
trade_id
allocation_id
position_effect
source_policy_id
source_policy_version
policy_evaluation_id
proposal_id
logical_order_id
physical_order_id
fill_id
fill_allocation_id
lot_id
disposition_id
```

`TradeIntent.trade_id` 在 `OPEN` 时是预分配的新交易 ID，在其他效果时是目标交易 ID。`ApprovedOrder` 和 `Fill` 可将其命名为 `target_trade_id`，但语义和数值必须保持一致。

## 8. 订单与成交模型

### 8.1 逻辑订单与物理订单

风险引擎批准逻辑订单，执行层生成物理订单。v1 冻结为一对一映射：

```text
LogicalApprovedOrder
→ PhysicalExecutionOrder
→ PhysicalFill
→ one FillAllocation
```

v1 强制：

- 不聚合同方向逻辑订单；
- 不对同一股票的跨策略买卖做内部净额结算；
- 每张物理订单独立计算最低佣金；
- 每笔物理成交仍生成一条 `FillAllocation`；
- 不因另一个逻辑订单更严格的价格约束阻止本订单成交。

物理聚合属于后续独立执行版本，必须重新定义价格兼容、部分成交、费用分摊并通过新的 golden master，不得通过配置在 v1 内开启。

### 8.2 ADD 单次有效规则

ADD 订单只在信号日后的下一个交易日有效：

```text
valid_session = next_trading_session(signal_asof)
```

该交易日未成交即过期，并释放全部现金和风险预留。旧 ADD 不得自动顺延；如后续仍满足条件，必须基于新的收盘快照重新运行插件、仲裁和风险审批。

### 8.3 费用归属

v1 一对一映射下，物理成交的全部费用直接归属唯一 `FillAllocation`：

```text
fill_allocation.allocated_commission_cash = physical_fill.commission
fill_allocation.allocated_transfer_fee_cash = physical_fill.transfer_fee
fill_allocation.allocated_stamp_tax_cash = physical_fill.stamp_tax
fill_allocation.allocated_slippage_cash = physical_fill.slippage_cost
```

卖出 `FillAllocation` 再按实际消耗数量把费用分配给 `LotDisposition`。尾差按照 `lot_id` 升序确定性分配，分配后必须满足费用守恒。

### 8.4 幂等键

评价、提案和逻辑 ADD 订单使用不同唯一键：

```text
evaluation_key
= (trade_id, signal_asof, policy_id, policy_version)

proposal_key
= (evaluation_id, add_sequence)

logical_add_order_key
= (trade_id, signal_asof, add_sequence, INCREASE)
```

`logical_add_order_key` 不包含 `policy_id`，确保不同单插件或组合插件不能对同一交易、同一信号日和同一序号创建多张 ADD 订单。

```text
add_sequence = add_count + 1
```

`add_count` 只在第一笔正数量 ADD 成交后增加。提案、批准、拒绝、取消和过期均不得增加。重复计算只能返回已有评价、提案或订单，不得重复创建现金和风险预留。

## 9. 退出优先与状态机

### 9.1 收盘处理顺序

```text
1. 处理当日收盘行情和已生效公司行为
2. 更新每个 LogicalTrade 的退出状态与动态止损
3. 先评估选股策略退出
4. 将命中退出的交易原子地置为 EXIT_REQUESTED
5. 取消该交易所有 AddProposal、ADD 订单和相关预留
6. 为退出交易生成 REDUCE/CLOSE 意图
7. 仅对仍为 ACTIVE 的逻辑交易运行 PositionAddPolicy
8. 仲裁加仓提案
9. 风险预审批并生成下一交易日订单
10. 保存完整审计快照
```

### 9.2 开盘处理顺序

```text
1. 入账当日可用的现金和股份权益
2. 重放取消和到期事件
3. 执行有效卖单
4. 更新卖出后的逻辑和物理持仓
5. 对每张 INCREASE 订单重新验证 target trade
6. 执行有效 OPEN 买单
7. 执行有效 INCREASE 买单
8. 分配成交、费用和 lots
9. 进行成交后风险复核
```

### 9.3 执行器硬约束

以下任一条件成立时，ADD 必须取消或拒绝：

- `trade_id` 不存在；
- 交易状态不是 `ACTIVE`；
- 存在 `pending_exit_order_ids`；
- `exit_requested = true`；
- ADD 序号超过 `max_add_count`；
- 已存在同一幂等键的订单或成交；
- 买入后会违反 T+1、现金、数量或风险约束；
- 订单的价格、有效期或数据版本与批准记录不一致。

推荐拒绝码：

```text
TARGET_TRADE_NOT_ACTIVE
EXIT_ALREADY_REQUESTED
PENDING_EXIT_ORDER
ADD_LIMIT_REACHED
DUPLICATE_ADD_REQUEST
ADD_ORDER_CANCELLED_BY_EXIT
ADD_ORDER_STALE_STATE
```

### 9.4 完整状态转移

| 事件 | 前置状态 | 后置状态 | 预留和账本处理 |
|---|---|---|---|
| OPEN 订单批准 | 无 | `PENDING_OPEN` | 预留现金、初始风险和同日风险 |
| OPEN 正数量成交 | `PENDING_OPEN` | `ACTIVE` | 释放计划预留，登记实际现金和风险，创建 OPEN lot |
| OPEN 拒绝、取消或过期 | `PENDING_OPEN` | `CANCELLED` | 释放全部现金和风险预留，不创建 lot |
| ADD 提案或批准 | `ACTIVE` | `ACTIVE` | 提案不改状态；批准后增加 ADD 现金与风险预留 |
| ADD 正数量成交 | `ACTIVE` | `ACTIVE` | 释放对应计划预留，登记实际冻结风险，创建 INCREASE lot，`add_count += 1` |
| ADD 拒绝、取消或过期 | `ACTIVE` | `ACTIVE` | 释放对应 ADD 现金与风险预留，`add_count` 不变 |
| EXIT 信号确认 | `ACTIVE` | `EXIT_REQUESTED` | 原子取消所有 ADD 提案和订单，释放 ADD 预留 |
| EXIT 订单批准 | `EXIT_REQUESTED` | `EXIT_PENDING` | 对具体可卖 lots 建立卖出数量预留 |
| EXIT 部分成交 | `EXIT_PENDING` | `EXIT_PENDING` | 创建 dispositions，减少 lot 与卖出预留，保留未退出数量 |
| EXIT 延期 | `EXIT_PENDING` | `EXIT_PENDING` | 保留有效卖出预留，不允许 ADD |
| EXIT 完全成交或终止处置 | `EXIT_PENDING` | `CLOSED` | 清空活动 lots 和卖出预留，冻结最终归因 |
| EXIT 订单取消但退出仍有效 | `EXIT_PENDING` | `EXIT_REQUESTED` | 释放被取消订单预留，等待生成新退出订单 |
| 风险引擎部分去风险成交 | `ACTIVE` | `ACTIVE` | 消耗指定 lots；若仍有数量则保持 ACTIVE |
| 风险引擎完全去风险成交 | `ACTIVE` | `CLOSED` | 消耗该 trade 全部 lots，取消其 pending ADD 并释放预留 |
| 收购、换股、退市等终止处置 | `ACTIVE/EXIT_REQUESTED/EXIT_PENDING` | `CLOSED` | 按生命周期事件结算或核销并释放所有预留 |

非法状态转移必须失败关闭并记录 `INVALID_TRADE_STATE_TRANSITION`。状态、订单集合和预留更新必须在同一账本事务内完成；任一步失败不得留下部分更新。

## 10. T+1、部分退出与卖出预留

### 10.1 可卖日期

每个买入 lot 必须保存 `sellable_date`。沪深 A 股普通买入的首个可卖日期是成交后的下一个有效交易日。公司行为新增股份使用事件数据给出的上市可交易日期；未知时失败关闭，不能假设立即可卖。

### 10.2 逻辑退出分配

退出订单先限定 `trade_id`，再从该交易可卖 lots 中分配。首期使用 FIFO：

```text
fill_date 升序
→ lot_id 升序
```

退出一个逻辑交易不得消耗其他逻辑交易的 lots。

### 10.3 卖出数量预留

创建卖单时对具体 lots 建立 `SellReservation`：

```text
SellReservation
├── logical_order_id
├── trade_id
├── lot_id
├── quantity
├── created_at
├── expires_at
└── status
```

延期卖单继续占用预留数量，避免同一股份被重复卖出。取消或成交后释放相应预留。

### 10.4 无法全部卖出

若交易包含当日买入、公司行为未到账或其他不可卖股份：

- 对可卖部分生成 `REDUCE`；
- 剩余部分保持 `EXIT_PENDING`；
- 下一交易日继续尝试；
- 不允许该交易在等待期间加仓；
- 报告未退出数量、账面估值和压力损失。

## 11. 公司行为

公司行为必须作用到事件基准日有权的具体 lots。

### 11.1 现金分红

- 按有权 lot 的股份数量分配现金；
- 真实现金在到账日进入物理资金账；
- 同时生成每个逻辑交易的归属事件；
- 物理现金增量必须等于全部逻辑分配之和。

### 11.2 送股、转增与拆并股

- 每个 lot 先计算理论调整后数量；
- 先分配整数部分；
- 余股按照小数余数降序、`lot_id` 升序确定性分配；
- 调整原始价空间的成本和止损；
- `initial_r_cash_frozen` 保持冻结；
- 新增股份使用明确的可卖日期。

### 11.3 收购、换股、退市和价值核销

相关事件必须保留原 `trade_id` 与 `lot_id` 的 lineage。新证券由事件生成新的物理持仓映射，不得作为普通 ADD。缺少可靠处置数据时使用既定保守估值和单独披露规则。

## 12. 价格空间与 R 定义

### 12.1 字段命名

所有跨价格空间字段必须显式命名：

```text
initial_entry_price_adjusted
initial_entry_price_raw
highest_close_adjusted
current_stop_adjusted
current_stop_raw
initial_r_per_share_adjusted
initial_r_per_share_raw_at_fill
initial_r_cash_frozen
adjustment_factor_signal
```

禁止使用无后缀的 `entry_price`、`highest_close`、`current_stop` 或 `initial_r` 作为持久化字段。

### 12.2 使用原则

- 趋势、突破、ATR 和历史窗口在冻结复权价空间计算；
- 成交、现金、费用、最大买入价和压力损失在原始价空间计算；
- 信号日保存复权因子与映射后的原始止损；
- 公司行为更新当前原始价格空间状态，不追溯改写冻结实验输入；
- 插件的 `trigger_snapshot` 保存窗口起止日期、输入值、价格空间和数据版本。

### 12.3 初始 R

基础交易成交时冻结：

```text
initial_r_per_share_raw_at_fill
= initial_fill_price_raw - initial_stop_raw_at_fill

initial_r_cash_frozen
= initial_r_per_share_raw_at_fill × initial_quantity
```

基础交易的历史 `1R` 不因加仓改变。每个加仓 lot 另存自己的 `risk_cash_frozen`，不能重算或摊入基础 `1R`。

## 13. 加仓插件接口

### 13.1 输入上下文

```text
PositionAddContext
├── signal_asof
├── trade_snapshot
├── physical_position_snapshot
├── lot_snapshots[]
├── pending_order_snapshots[]
├── adjusted_market_window
├── raw_execution_snapshot
├── portfolio_risk_snapshot
├── base_signal_status
├── field_coverage
└── data_version
```

上下文为只读不可变对象。插件不得通过 executor、risk engine 或全局状态读取未来数据。

### 13.2 评价结果

每次调用必须返回：

```text
PolicyEvaluation
├── evaluation_id
├── policy_id
├── policy_version
├── trade_id
├── signal_asof
├── triggered
├── evaluated_inputs
├── reason_codes
├── missing_fields
├── config_sha256
├── data_version
└── proposal
```

`proposal` 在未触发时为空，但其他审计字段仍须完整保留。

### 13.3 加仓提案

```text
AddProposal
├── proposal_id
├── evaluation_id
├── trade_id
├── allocation_id
├── symbol
├── signal_asof
├── add_sequence
├── trigger_code
├── risk_budget_cash_cap
├── notional_cash_cap
├── quantity_cap_optional
├── current_stop_raw_snapshot
├── max_buy_price_raw
├── valid_session
├── priority
└── reason_codes
```

首期插件不得提出新的止损。`current_stop_raw_snapshot` 必须等于逻辑交易已经发布的当前止损，只用于风险审批和状态一致性检查。

## 14. 首期插件

### 14.1 `NoAddPolicy`

始终返回：

```text
triggered = false
reason_codes = [POLICY_DISABLED_BY_DEFINITION]
proposal = null
```

它用于验证新账本和新执行路径在不加仓时逐笔复现现有结果。

### 14.2 `ProtectedWinnerPolicy`

名称表示“止损价已提升到盈亏平衡附近的盈利持仓”，不表示一定能按止损成交。审计触发码使用 `STOP_LEVEL_AT_BREAKEVEN`。

默认 v1 条件全部满足才触发：

```text
trade.status == ACTIVE
add_count == 0
holding_sessions >= 5
sessions_since_last_fill >= 5
base_signal_status == HOLD
close_adjusted >= initial_entry_price_adjusted
                  + 1.0 × initial_r_per_share_adjusted
close_adjusted >= last_fill_price_adjusted + 0.5 × ATR21_adjusted
current_stop_raw >= breakeven_price_raw_including_costs
no pending ADD
no pending or requested EXIT
required fields complete
```

含费用盈亏平衡价至少覆盖：

```text
历史买入金额
+ 已发生买入费用
+ 预计卖出佣金、过户费和印花税
+ 预计卖出滑点
```

默认风险上限：

```text
add_risk_budget_fraction = 0.125%
add_budget_base = ENTRY_EQUITY
max_add_count = 1
max_add_notional_fraction = 2%
```

其中 `sessions_since_last_fill` 使用 `last_buy_fill_session_index` 计算，首次 OPEN 和每次成功 INCREASE 都更新该字段；拒绝、取消和过期订单不更新。

2% 名义上限使用 `signal_asof` 收盘后的全局组合净值，不使用 `allocation_id` 的归属净值：

```text
notional_cash_cap
= portfolio_equity_asof × max_add_notional_fraction
```

v1 的 `allocation_id` 只负责资本和绩效归属，不拥有独立现金账。独立资本袖套属于后续版本。

以上是首期冻结候选，不得根据完整样本结果自动搜索。

### 14.3 后续插件

`RebreakoutPolicy`：

```text
close_adjusted[t] > max(high_adjusted[t-20:t])
```

窗口为前 20 个交易日，不包含当前日。成交额确认等增强条件必须作为独立版本，不得在观察回测结果后静默加入。

`TurtleAtrPolicy`：价格相对上次成交向有利方向移动 `0.5 × ATR21` 后提出一个波动调整单位。首个研究版本仍限制最多一次 ADD。

`PullbackReclaimPolicy` 参数自由度较高，不进入首期实施范围。

## 15. 组合插件与提案仲裁

### 15.1 支持模式

```text
ALL_OF
ANY_OF
PRIORITY
```

组合只能对同一 `trade_id`、同一 `signal_asof` 的评价进行仲裁。

### 15.2 `ALL_OF`

所有成员插件均触发才产生组合提案。约束合并为：

```text
risk_budget_cash_cap = min(member caps)
quantity_cap = min(non-null member caps)
max_buy_price_raw = min(member max prices)
valid_session = common next trading session
priority = configured composite priority
```

首期插件不能修改止损，因此不存在多个插件止损合并问题。

### 15.3 `ANY_OF`

任一成员触发即可提交，但同一交易同一时点只能形成一张逻辑 ADD 订单。多个触发提案使用预先冻结的优先级；不能在回测后选择表现更好的插件。

### 15.4 `PRIORITY`

按照冻结顺序选择首个触发插件。所有被压制提案仍写入审计记录，原因码为 `SUPPRESSED_BY_HIGHER_PRIORITY_POLICY`。

### 15.5 首期组合范围

首期不实现组合插件。单插件验证完成后，首个组合候选为：

```text
ProtectedWinnerPolicy ALL_OF RebreakoutPolicy
```

## 16. 风险审批

### 16.1 单股剩余额度

```text
symbol_headroom_cash
= equity × max_symbol_weight
 - existing_symbol_market_value
 - pending_symbol_buy_value

quantity_symbol_headroom
= floor_to_board_lot(symbol_headroom_cash / max_buy_price_raw)
```

### 16.2 逻辑交易累计加仓预算

```text
trade_add_risk_headroom
= add_risk_budget_cash_frozen
 - filled_add_risk_cash_frozen
 - reserved_add_risk_cash
```

默认 `add_risk_budget_cash_frozen` 在基础交易首次成交时按照 `ENTRY_EQUITY` 冻结，避免净值上升或多次调用不断扩大预算。

### 16.3 加仓每股风险

```text
planned_add_risk_per_share
= max_buy_price_raw - current_stop_raw_snapshot
```

若结果不是有限正数，拒绝并记录 `NO_EXECUTABLE_ADD_R`。压力模型独立计算跳空和连续跌停损失，止损达到盈亏平衡不能替代压力审批。

### 16.4 名义金额上限

```text
quantity_notional_cap
= floor_to_board_lot(notional_cash_cap / max_buy_price_raw)
```

`notional_cash_cap` 来自已仲裁提案，默认使用信号日全局组合净值的 2%。风险引擎必须验证提案使用的净值快照与风险审批快照一致。

### 16.5 最终数量

```text
final_quantity = min(
    proposal_quantity_cap,
    quantity_notional_cap,
    add_risk_budget_quantity,
    trade_add_risk_headroom_quantity,
    symbol_headroom_quantity,
    gross_exposure_headroom_quantity,
    portfolio_open_risk_headroom_quantity,
    industry_open_risk_headroom_quantity,
    same_day_risk_headroom_quantity,
    capacity_quantity,
    cash_quantity,
    stress_quantity,
)
```

所有数量向下取 100 股整数。每个中间数量和决定性约束都写入风险决策。

### 16.6 ADD 风险预留结算

批准时：

```text
planned_add_risk_cash
= planned_add_risk_per_share × approved_quantity

reserved_add_risk_cash
+= planned_add_risk_cash
```

正数量成交时：

```text
reserved_add_risk_cash
-= planned_add_risk_cash

actual_add_risk_cash
= max(0, fill_price_raw - current_stop_raw_snapshot)
   × filled_quantity

filled_add_risk_cash_frozen
+= actual_add_risk_cash
```

拒绝、取消或过期时：

```text
reserved_add_risk_cash
-= planned_add_risk_cash

filled_add_risk_cash_frozen unchanged
add_count unchanged
```

成交后计划预留全部释放，实际成交风险进入冻结累计值。未成交差额不得残留。历史已成交 ADD 风险不因移动止损、部分退出或公司行为追溯释放。

### 16.7 开放风险聚合

每个逻辑交易独立计算：

```text
logical_open_risk
= max(0, mark_price_raw - logical_current_stop_raw)
   × logical_quantity
```

随后按 symbol、industry 和 portfolio 聚合。可另设股票级灾难退出，但不能用一个物理止损覆盖各策略的逻辑止损。

### 16.8 成交后去风险顺序

成交后风险复核发现突破时，按以下确定顺序处理：

1. 取消尚未成交的新增风险订单并释放预留；
2. 优先选择本次开盘新增的 ADD lots；
3. 其余 ADD lots 优先于基础 OPEN lots；
4. 同一层按边际风险贡献降序；
5. 再按 `fill_date` 降序、`trade_id` 升序、`lot_id` 升序；
6. 生成带明确 `trade_id` 和目标 lots 的 `REDUCE/CLOSE`，不得按 symbol 清空所有逻辑交易。

A 股 T+1 下，当日新成交 lot 不得当日卖出。系统只能立即取消其他未成交订单、记录风险突破，并为最早可卖日期生成去风险计划；在此之前继续把该 lot 计入开放风险和连续跌停压力。

## 17. 公司行为后的风险与 lineage

- 当前原始价空间止损按公司行为因子更新；
- lots 数量和每股成本按事件规则调整；
- 历史现金风险保持冻结并继续归属于原 lot；
- 组合当前开放风险使用调整后的数量和当前止损重新计算；
- 公司行为生成独立 `PositionEvent` 和逻辑分配记录；
- 所有调整前后值、事件来源和生效日期必须可追溯。

## 18. 实验设计

### 18.1 Golden master：`NoAddPolicy`

新架构关闭加仓时，必须与冻结基线比较：

- 交易意图；
- 风险决策；
- 订单和成交；
- 成交价、数量、费用和滑点；
- 现金曲线和每日净值；
- 持仓事件；
- 退出日期和退出原因；
- 总收益、最大回撤和压力结果。

差异必须逐项解释，不能只要求最终收益接近。

#### 18.1.1 冻结基线身份

每个 golden master 基线必须保存：

```text
git_commit
working_tree_patch_sha256
data_snapshot_sha256
prediction_snapshot_sha256
risk_config_sha256
execution_config_sha256
start_date
end_date
dynamic_stop_sync
execution_mode
price_limit_model_version
slippage_bps
runtime_versions
```

工作树存在未提交修改时必须保存补丁哈希，不能只记录 `HEAD`。基线目录只读保留，不得被新运行覆盖。

#### 18.1.2 新旧 schema 映射

```text
legacy Position
→ one PhysicalPosition
 + one LogicalTrade
 + one OPEN PositionLot

legacy ApprovedOrder
→ one LogicalApprovedOrder
 + one PhysicalExecutionOrder

legacy Fill
→ one PhysicalFill
 + one FillAllocation

legacy partial sell
→ one sell FillAllocation
 + one or more FIFO LotDisposition
```

新增 lineage 字段不要求与旧 schema 中不存在的字段比较，但必须由冻结输入确定性生成。迁移记录保存旧记录 ID 与新业务键的映射。

#### 18.1.3 规范化排序

比较前使用稳定排序：

```text
intents: (signal_asof, strategy_id, symbol, side, intent_id)
orders:  (valid_session, side_priority, strategy_id, symbol,
          position_effect, logical_business_key)
fills:   (date, side_priority, strategy_id, symbol,
          position_effect, logical_business_key)
curve:   (date)
events:  (date, symbol, event_type, trade_id, lot_id)
```

`side_priority` 沿用冻结执行模式。新随机 ID 不参与经济记录排序。

#### 18.1.4 字段与容差

- 日期、symbol、side、数量、状态、原因码和退出原因：精确相等；
- 价格、费用、现金、持仓市值和风险现金：绝对误差不超过 `1e-8`；
- 收益率和回撤：绝对误差不超过 `1e-12`；
- 每日曲线日期集合必须完全相同；
- 旧 ID 不要求等于新 ID；同一新输入重复运行产生的新业务 ID 必须完全一致；
- 新 schema 的数量、费用、现金和 lot 守恒必须额外通过。

若迁移实现引入货币分单位取整，应单独建立新执行版本，不能放宽原 golden master 容差掩盖差异。

#### 18.1.5 模式矩阵

Alpha158 和 VCP 分别冻结以下模式，不能跨模式比较：

```text
dynamic_stop_sync = false / true
execution_mode = frozen baseline mode
slippage_bps = frozen baseline value
risk_config = frozen baseline hash
price_limit_model = frozen baseline version
```

`NoAddPolicy` 只有通过对应模式的订单、成交、费用、现金、曲线和风险决策比较，才允许进入下一里程碑。

### 18.2 固定基础路径 overlay

目的：诊断加仓触发是否选择了有利的持仓路径。

必须冻结：

- 基础入场股票、日期、价格和数量；
- 基础退出日期和价格；
- 基础动态止损与退出路径；
- 基础费用和现金曲线。

虚拟加仓独立计算费用和滑点，不占用基础账户现金，不得并入普通账户净值。只报告：

```text
incremental_pnl
incremental_r
return_on_fixed_overlay_capital
return_on_add_risk_budget
MFE / MAE
holding_period
fees / slippage
```

### 18.3 完整可执行回放

目的：判断加仓进入真实现金和风险约束后，整个组合是否改善。

ADD 会占用现金、风险、单股额度和行业额度，也可能导致后续首次入场被拒绝。该实验报告普通组合总收益、回撤、仓位、换手、压力损失和拒单归因。

### 18.4 实验矩阵

首期：

| 实验 | 选股策略 | 加仓插件 |
|---|---|---|
| A0 | Alpha158 | NoAdd |
| A1 | Alpha158 | ProtectedWinner |
| V0 | VCP | NoAdd |
| V1 | VCP | ProtectedWinner |

后续在单插件正确后增加 `Rebreakout`、`TurtleATR` 和组合插件。Alpha158 与 VCP 的组合实验必须在多逻辑交易归属测试通过后进行。

### 18.5 匹配随机加仓基线

随机基线保持：

- 相同加仓日期；
- 相同加仓次数；
- 相同风险或名义资金；
- 相近持仓年龄；
- 相近浮盈 R 区间；
- 相近流动性、行业和市场状态；
- 相同基础退出路径。

仅使用 `signal_asof` 已知字段匹配。生成数百至数千个重复组合，报告候选策略在分布中的分位数和置信区间。

## 19. 绩效与归因

每次实验至少输出：

### 19.1 组合层

- 总收益、年化收益和最大回撤；
- 平均与峰值股票仓位；
- 开放风险、行业风险和同日新增风险；
- 1、3、5 个连续跌停压力损失；
- 换手、佣金、税费和滑点；
- 因 ADD 占用资源而拒绝的基础入场数量。

### 19.2 逻辑交易层

- 选股策略和版本；
- 加仓策略和版本；
- 基础 lots 收益；
- ADD lots 收益；
- 加仓前后 MFE、MAE 和 R；
- 退出原因；
- 是否存在多策略同股持仓。

### 19.3 插件层

- 评价次数；
- 各未触发原因数量；
- 提案数、批准数、成交数和取消数；
- 风险引擎各限制裁剪数量；
- 加仓批次成本后收益和利润因子；
- 收益在年份、股票、行业和市场状态上的集中度。

## 20. 可视化

交易 K 线页面应区分：

- 基础买入：绿色标记；
- ADD：蓝色标记并显示插件和触发条件；
- 动态止损：按逻辑交易绘制；
- 部分退出：橙色标记；
- 完全退出：红色标记；
- 公司行为：独立事件标记。

同一股票存在多个逻辑交易时，页面提供按 `trade_id`、选股策略和加仓策略筛选，物理持仓视图另行显示聚合数量。

## 21. 测试要求

### 21.1 单元测试

- ID 稳定性和 schema 校验；
- `position_effect` 合法组合；
- `FillAllocation` 与 `LotDisposition` schema 和守恒；
- lot 创建、FIFO 部分退出和余额；
- 全部合法和非法状态转移；
- T+1 可卖数量；
- 卖出预留和延期；
- 卖出费用按 dispositions 分摊与舍入尾差；
- 公司行为数量、成本、止损和余股分配；
- 单股剩余额度、2% 名义额度和累计加仓预算；
- ADD 批准、成交、取消、拒绝和过期的风险结算；
- ADD 只在下一交易日有效；
- 插件原因码和缺失数据失败关闭；
- 仲裁的 ALL_OF、ANY_OF 和 PRIORITY 规则；
- 分层 ADD 幂等键和成交后 `add_count`。

### 21.2 集成测试

- Alpha158 和 VCP 同时持有同一股票；
- 一个逻辑交易退出而另一个继续持有；
- EXIT 与 ADD 同日冲突；
- 延期卖单期间禁止 ADD；
- ADD 成交前目标交易已关闭；
- ADD 后触发 T+1 限制；
- ADD 与公司行为相邻发生；
- 同股多逻辑订单分别执行且不内部净额；
- 物理现金、持仓与全部逻辑分配守恒；
- 成交后风险复核按 trade 和 lots 确定性去风险；
- 当日新增 ADD 因 T+1 无法立即去风险时正确披露风险。

### 21.3 无前视测试

- 修改 `signal_asof` 之后的数据，不得改变当日插件评价；
- 次日开盘跳空不得改变前一日批准数量；
- 突破窗口不包含当前日；
- 公司行为只能在其有效和可用时点进入状态。

### 21.4 Golden master

`NoAddPolicy` 模式逐项比较冻结基线。至少覆盖 Alpha158、VCP、公司行为、延期卖出和期末持仓。

## 22. 验收门槛

### 22.1 架构验收

- 物理数量等于全部活动 lots 剩余数量之和；
- 物理现金变化等于全部成交、费用和公司行为现金流之和；
- 每笔订单和成交均可追溯到逻辑交易；
- 一个策略退出不会错误清空另一策略的逻辑持仓；
- 退出请求不会遗留可成交 ADD；
- 物理成交、逻辑分配、lot dispositions、费用和现金全部守恒；
- ADD 过期后不存在残留现金或风险预留；
- 成交后去风险不会按 symbol 错误清空其他逻辑交易；
- 所有风险上限按剩余额度计算；
- `NoAddPolicy` 通过逐笔 golden master。

### 22.2 策略研究验收

加仓策略只有同时满足以下条件才可进入前瞻模拟盘候选：

1. ADD lots 独立成本后净收益为正；
2. 完整可执行组合收益改善；
3. 最大回撤相对基线恶化不超过预先冻结的容忍值；
4. 连续跌停压力不突破风险配置；
5. 增量收益不集中于极少数股票、行业或年份；
6. 在滚动样本外和匹配随机加仓基线中仍有优势；
7. 基础选股信号和参数没有因加仓结果被重新选择；
8. 参数、数据、代码和实验注册记录完整。

v1 最大回撤相对 NoAdd 基线恶化容忍值冻结为 1 个百分点。其他统计和样本门槛见第 25 节，不能在看完完整结果后补写。

## 23. 迁移与兼容

1. 新 schema 使用独立版本号，不原地解释旧产物；
2. 默认加载 `NoAddPolicy`；
3. 旧 `positions[symbol]` 读取接口可以提供只读聚合兼容层；
4. 旧 `Position` 不再作为新的事实源，只作为迁移输入；
5. 所有新功能使用显式开关，关闭时不得改变冻结基线；
6. 旧报告继续可读，新报告增加 trade、lot、policy 和 allocation 维度；
7. 数据与回测产物保存 schema 版本和配置哈希。

## 24. 实施顺序

本设计冻结后，开发计划按以下顺序拆分：

1. 冻结现有 Alpha158、VCP 的订单、成交、风险和净值基线；
2. 引入 lineage 和三层持仓模型；
3. 完成 T+1、部分退出、卖出预留、公司行为和费用分摊；
4. 实现 `position_effect` 与退出优先状态机；
5. 修正 ADD 的风险剩余额度；
6. 实现 `PolicyEvaluation`、`AddProposal` 和 `PositionAddPolicy`；
7. 实现 `NoAddPolicy` 并通过逐笔 golden master；
8. 实现并验证 `ProtectedWinnerPolicy`；
9. 运行固定路径 overlay 和完整可执行回放；
10. 单插件验证完成后再增加 Rebreakout、TurtleATR 和组合仲裁；
11. 最后扩展多选股策略组合实验和交易可视化。

## 25. 冻结配置与 M0 登记项

### 25.1 v1 已冻结配置

- `allocation_id` 默认值为 `GLOBAL`，v1 不维护独立袖套现金账；
- v1 禁止物理订单聚合和跨策略内部净额；
- 金额继续使用现有双精度口径，不新增分币取整；费用分配尾差全部归入 `lot_id` 排序后的最后一个 disposition，保证精确守恒；
- 公司行为新增股份使用事件数据给出的可交易日期；缺失时失败关闭，不假设立即可卖；
- v1 部分退出固定 FIFO，不允许策略覆盖；
- 加仓风险预算固定使用 `ENTRY_EQUITY`；
- ProtectedWinner 固定为 5 日、1R、0.5 ATR21、0.125% 风险和 2% 全局净值名义上限；
- 最大回撤相对 NoAdd 基线恶化不得超过 1 个百分点；
- matched placebo 固定运行 1,000 次；
- 历史实验至少需要 30 次实际 ADD 才能形成有效性判断，少于 30 次只报告案例和覆盖；
- 多插件比较使用 Holm 方法控制多重检验；
- 当前历史已被反复观察，只作为研究样本，最终部署判断必须增加新的前瞻模拟盘。

### 25.2 M0 必须登记

- Alpha158 与 VCP 的代码、脏工作树补丁、配置、预测和数据哈希；
- 动态止损、执行模式、费用、滑点和涨跌停模型版本；
- 滚动样本外切分与随机种子；
- matched placebo 的行业、流动性、持仓年龄和浮盈 R 匹配容差；
- golden master 每张表的基线文件和规范化业务键。

这些登记必须在读取加仓实验结果前完成。M0 可以补充运行身份和匹配容差，不能修改第 25.1 节的策略参数与风险门槛。
