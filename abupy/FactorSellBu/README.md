# FactorSellBu 模块开发说明

FactorSellBu 定义持仓后的卖出规则。卖出因子由 AlphaBu 按交易日调用，对当前订单集合判断止盈、止损、趋势退出或固定持有期退出。

## 基类

`AbuFactorSellBase` 定义初始化、订单筛选和卖出方法；`AbuFactorSellXD` 提供固定窗口行情。`ESupportDirection` 声明因子支持买涨、买跌或两个方向，新增因子必须正确限制方向。

## 内置因子

| 类 | 用途 |
| --- | --- |
| `AbuFactorAtrNStop` | 按买入时 ATR 设置止盈止损倍数 |
| `AbuFactorPreAtrNStop` | 以单日变化和前日 ATR 控制风险 |
| `AbuFactorCloseAtrNStop` | 从持仓后最高收益回落时保护利润 |
| `AbuFactorSellBreak` | 跌破窗口低点退出 |
| `AbuDoubleMaSell` | 双均线卖出 |
| `AbuFactorSellNDay` | 持有指定交易日后卖出 |

## 配置格式

```python
sell_factors = [
    {'class': AbuFactorSellBreak, 'xd': 20},
    {'class': AbuFactorAtrNStop,
     'stop_loss_n': 0.5, 'stop_win_n': 3.0},
]
```

`g_default_pre_atr_n` 和 `g_default_close_atr_n` 是部分因子的模块默认值。生产研究应在配置中显式传入关键参数，避免全局值变化影响实验复现。

## 新因子要求

- 在 `_init_self` 中校验参数，在 `fit_day` 中处理订单列表。
- 使用基类卖出方法更新订单，保留 `sell_type` 和 `sell_type_extra` 的可解释信息。
- 同时测试盈利、亏损、未平仓、买涨和买跌订单。
- 多个卖出因子可能在同一天命中，需确认调用顺序和已卖出订单过滤逻辑。
- 若因子只服务某个买入因子，应作为附属卖出规则配置，避免影响无关订单。

## 相关模块

[FactorBuyBu](../FactorBuyBu/README.md) 创建订单，[AlphaBu](../AlphaBu/README.md) 调度卖出因子，[TradeBu](../TradeBu/README.md) 将卖出结果转换为资金动作。
