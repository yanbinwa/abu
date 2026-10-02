# SlippageBu 模块开发说明

SlippageBu 将策略信号映射为模拟成交价格，并处理涨跌停等无法成交情形。当前实现基于日线高低价，适用于研究回测，不模拟盘口深度、订单延迟或大额冲击成本。

## 文件和类

| 文件 | 作用 |
| --- | --- |
| `ABuSlippageBuyBase.py` | 买入成交价基类与涨停过滤装饰器 |
| `ABuSlippageBuyMean.py` | 以当日最高价和最低价均值模拟买入 |
| `ABuSlippageSellBase.py` | 卖出成交价基类与跌停过滤装饰器 |
| `ABuSlippageSellMean.py` | 以当日最高价和最低价均值模拟卖出 |

## 关键设置

- `g_enable_limit_up` 与 `g_enable_limit_down` 控制是否模拟涨跌停成交概率。
- `g_limit_up_deal_chance` 与 `g_limit_down_deal_chance` 控制概率参数。
- `g_pre_limit_up_rate` 与 `g_pre_limit_down_rate` 控制临近涨跌停阈值。
- `g_open_down_rate` 控制默认买入模型对大幅低开的过滤。

这些是模块级变量，多进程执行前修改时应确认环境传播和随机性复现。

## 新成交模型

分别继承买入或卖出基类并实现 `fit_price`。返回有限价格表示可成交；基类约定的无效值表示放弃成交。实现应明确使用哪一交易日、是否使用未来信息、停牌和价格边界如何处理。

分钟级或实盘模拟不应继续复用日线均价假设，应引入时间、成交量、盘口和订单状态模型。

## 相关模块

[FactorBuyBu](../FactorBuyBu/README.md) 选择买入滑点，[FactorSellBu](../FactorSellBu/README.md) 选择卖出滑点，[TradeBu](../TradeBu/README.md) 使用最终价格执行动作。
