# BetaBu 模块开发说明

BetaBu 负责把买入信号转换为仓位比例。它根据资金、价格、波动率或策略统计确定买入数量，是信号层与资金执行层之间的风险控制接口。

## 内置仓位

| 类 | 计算依据 |
| --- | --- |
| `AbuPositionBase` | 仓位基类和市场参数入口 |
| `AbuAtrPosition` | ATR 波动率，波动越高仓位通常越低 |
| `AbuKellyPosition` | 胜率和盈亏比对应的 Kelly 比例 |
| `AbuPtPosition` | 当前价格在历史区间的位置 |

## 关键设置

| 设置 | 默认值 | 含义 |
| --- | ---: | --- |
| `g_pos_max` | 0.75 | 单次仓位比例上限 |
| `g_deposit_rate` | 1 | 保证金或资金占用倍率 |
| `g_default_pos_class` | `None` | 全局默认仓位配置 |
| `g_atr_pos_base` | 0.1 | ATR 仓位基础比例 |

因子未指定仓位时，买入基类默认使用 `AbuAtrPosition`。全局配置会影响所有后续因子实例，研究代码应记录修改值。

## 新仓位实现

继承 `AbuPositionBase`，在 `_init_self` 中读取参数，并实现 `fit_position` 返回仓位比例。实现需要处理价格无效、ATR 为零、资金不足和极端统计值，并受全局最大仓位约束。

仓位函数只负责比例或数量决策，不负责成交价、手续费和资金表更新。保证金市场若仅修改 `g_deposit_rate` 仍不足以覆盖逐日盯市等完整规则。

## 相关模块

[FactorBuyBu](../FactorBuyBu/README.md) 选择仓位类，[TradeBu](../TradeBu/README.md) 执行资金动作，[SlippageBu](../SlippageBu/README.md) 决定模拟成交价。
