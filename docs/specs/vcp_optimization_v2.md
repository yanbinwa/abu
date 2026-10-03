# VCP 优化实验 v2 规范

| 字段 | 内容 |
|---|---|
| 状态 | Implemented；正式验证未通过 |
| 创建日期 | 2026-10-03 |
| 基线提交 | `43ffbee` |
| 数据快照 | `sha256:41e541057ccce0d0a136420912cd9efdc15ac5d5f4638420529f08f27151ad9c` |

## 1. 目的

正式验证显示，`vcp_core_v1` 的选股优势不足，交易成本覆盖了微弱的成本前收益，完整事件退出增加了换手和损失。本轮只验证两个预先声明的假设：

1. 将退出规则逐项隔离，可以识别造成损失的规则；
2. 只在 VCP 候选中保留中期残差动量为正的股票，可以形成比单独 VCP 更严格的混合信号。

本轮不搜索窗口、阈值、权重、止损倍数或风险预算。

## 2. 风险口径修正

`d_core_r_fixed20` 使用初始止损距离计算 R 仓位，但固定持有期间不执行初始止损。它保留为历史诊断基线，不具备前瞻准入资格。

所有新的 R 仓位候选都必须启用 `INITIAL_STOP`。止损在收盘后确认，下一交易日通过统一卖出模型执行；跌停或停牌时延续卖单。

## 3. 退出消融

退出优先级保持：

```text
INITIAL_STOP
BREAKOUT_FAILURE
TRAILING_STOP
MARKET_REGIME
STAGNATION
FIXED_HOLD
```

冻结以下实验：

| 实验 | 启用规则 | 用途 |
|---|---|---|
| `g_stop_fixed20` | 初始止损 + 最长20日 | 可执行 R 基线 |
| `g_stop_breakout_fixed20` | 初始止损 + 前5日突破失败 + 最长20日 | 突破失败增量 |
| `g_stop_trailing_fixed20` | 初始止损 + 1R后3ATR跟踪 + 最长20日 | 跟踪止损增量 |
| `g_stop_market_fixed20` | 初始止损 + 市场跌破MA200 + 最长20日 | 市场退出增量 |
| `g_stop_trailing_stagnation` | 初始止损 + 跟踪止损 + 20日停滞退出 | 让盈利仓位继续运行 |

消融结果仅用于归因，不从中选择历史收益最高者作为正式候选。

## 4. 混合信号 `vcp_residual_v2`

VCP 硬条件、止损、最高买价和风险配置完全沿用 `vcp_core_v1`。新增中期残差动量，公式沿用已有 `residual_momentum` 的冻结定义：

```text
beta estimation returns: t-251 ... t-21
formation returns:        t-125 ... t-21
minimum beta observations:      200
minimum formation observations: 95
residual_momentum = sum(stock_return - beta * market_return)
```

只保留 `residual_momentum > 0` 的 VCP 信号。横截面排序使用四个分量的等权百分位排名：

```text
hybrid_score = 0.25 * rank(residual_momentum)
             + 0.25 * rank(ma120_log_slope)
             + 0.25 * rank(vcp_tightness)
             + 0.25 * rank(breakout_strength)
```

所有窗口只读取信号日及以前的数据。相同分数继续按股票代码升序打破平局。

## 5. 预先指定的主候选

主候选固定为：

```text
h_residual_stop_trailing_stagnation
= vcp_residual_v2
+ risk_v1
+ INITIAL_STOP
+ TRAILING_STOP
+ STAGNATION
```

同时运行 `h_residual_stop_fixed20`，用于区分混合信号贡献和自适应退出贡献。正式 placebo 必须验证预先指定的主候选，不允许根据消融结果更换候选。

## 6. 验证门槛

主候选必须同时满足：

- 成本后累计收益为正；
- 交易簇 95% bootstrap 下界为正；
- 1,000 组匹配 placebo 中进入前10%；
- 不少于100笔已闭合交易或60个独立入场簇；
- 三跌停压力清算最大回撤不超过20%；
- 无未退出持仓和账本不平；
- 新信号与 placebo 使用相同退出、风险和执行路径。

若任一门槛失败，版本保持 `research_only`，不得通过调整本规范参数重新解释同一数据。
