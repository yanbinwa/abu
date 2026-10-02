# FactorBuyBu 模块开发说明

FactorBuyBu 定义买入择时因子。因子在 AlphaBu 的逐日事件循环中读取当前及历史行情，生成下一交易日的方向性买入订单，并可组合仓位、滑点、选股、卖出和 UMP 规则。

## 基类和方向

`AbuFactorBuyBase` 是通用基类；`AbuFactorBuyTD` 和 `AbuFactorBuyXD` 分别提供双日及固定窗口辅助。具体因子必须混入一个方向类：`BuyCallMixin` 表示价格上涨获利，`BuyPutMixin` 表示价格下跌获利。这里的 call 和 put 是收益方向，不代表完整期权合约。

## 内置因子

| 文件 | 代表类 | 策略含义 |
| --- | --- | --- |
| `ABuFactorBuyBreak.py` | `AbuFactorBuyBreak`、`AbuFactorBuyPutBreak` | 突破窗口高点或低点 |
| `ABuFactorBuyDM.py` | `AbuDoubleMaBuy` | 动态双均线 |
| `ABuFactorBuyTrend.py` | `AbuUpDownTrend`、`AbuDownUpTrend` | 长短周期趋势与均值回复 |
| `ABuFactorBuyWD.py` | `AbuFactorBuyWD` | 星期维度胜率和收益条件 |
| `ABuFactorBuyDemo.py` | 多个教学因子 | 因子、UMP 和数字货币示例 |
| `ABuBuyFactorWrap.py` | `AbuLeastPolyWrap` | 为因子增加多项式趋势条件 |

## 配置格式

```python
buy_factors = [
    {'class': AbuFactorBuyBreak, 'xd': 60},
]
```

配置字典中的 `class` 指定类型，其余字段传给构造函数。可通过 `position`、`slippage`、附属卖出因子和附属选股因子覆盖默认行为。

## 新因子要求

1. 继承合适的基类并混入一个方向类。
2. 在 `_init_self` 中读取并校验参数，设置可识别的 `factor_name`。
3. 在 `fit_day` 中只判断信号，不直接修改资金表。
4. 使用 `buy_tomorrow` 或基类订单方法，避免用当日收盘信号模拟当日成交。
5. 明确历史窗口不足、停牌、涨跌停和重复信号的行为。
6. 用独立时间区间验证，避免把调参结果当作样本外结论。

## 相关模块

[AlphaBu](../AlphaBu/README.md) 驱动因子，[BetaBu](../BetaBu/README.md) 与 [SlippageBu](../SlippageBu/README.md) 决定数量和价格，[UmpBu](../UmpBu/README.md) 可拦截订单。
