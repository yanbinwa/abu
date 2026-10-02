# IndicatorBu 模块开发说明

IndicatorBu 提供常用技术指标的计算和绘图封装。接口既可接受标准 K 线表，也可根据订单自动截取交易前后行情，用于策略研究和结果解释。

## 指标索引

| 文件 | 指标和入口 |
| --- | --- |
| `ABuNDAtr.py` | ATR 14、ATR 21 及归一化范围 |
| `ABuNDMa.py` | 简单和指数移动平均线 |
| `ABuNDMacd.py` | MACD |
| `ABuNDRsi.py` | RSI |
| `ABuNDBoll.py` | 布林带 |
| `ABuNDBase.py` | 计算方式和订单绘图公共流程 |

## 输入约定

K 线表至少需要指标所依赖的 `close`、`high`、`low` 和日期索引。订单绘图依赖 `symbol`、买卖日期、卖出状态和结果字段，并会通过 MarketBu 重新获取扩展区间行情。

## 关键设置

`ABuNDBase.g_calc_type` 当前固定为 pandas 实现，TA-Lib 路径没有启用。`ABuNDRsi.g_rsi_gain` 控制 RSI 涨幅处理方式。研究报告应记录非默认修改。

## 新指标实现

- 分离纯计算函数和绘图函数，纯计算函数不得修改输入表。
- 支持从 K 线和订单调用时，复用 `plot_from_order` 的时间范围逻辑。
- 明确窗口、最小样本数和缺失值位置。
- 不在指标函数内解释交易方向；策略含义应由因子决定。
- 与已有命名保持一致，绘图入口使用 `plot_xxx_from_klpd` 和 `plot_xxx_from_order`。

## 相关模块

[MarketBu](../MarketBu/README.md) 提供标准 K 线，[FactorBuyBu](../FactorBuyBu/README.md) 和 [FactorSellBu](../FactorSellBu/README.md) 可使用指标，[TLineBu](../TLineBu/README.md) 提供更高层的趋势分析。
