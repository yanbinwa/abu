# MetricsBu 模块开发说明

MetricsBu 负责把回测结果转换为收益、风险和交易统计，并提供参数搜索、评分和多股票池验证。指标计算发生在交易执行之后，输入应是已经过资金约束的结果。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuMetricsBase.py` | 股票策略通用绩效和图表 |
| `ABuMetricsFutures.py` | 期货结果扩展 |
| `ABuMetricsTC.py` | 数字货币结果扩展 |
| `ABuMetricsScore.py` | 多指标标准化和综合评分 |
| `ABuGridSearch.py` | 策略参数组合搜索 |
| `ABuGridHelper.py` | 因子组合和评分表辅助 |
| `ABuCrossVal.py` | 按相关性区间抽样股票池并重复回测 |

## 主要指标

`AbuMetricsBase.fit_metrics` 计算策略与基准区间收益、年化收益、波动率、夏普比率、信息比率、Alpha、Beta、最大回撤、胜率、盈亏比和手续费等统计。

当前年化收益采用区间收益按交易天数线性换算，不是复合年增长率。新增报表或与其他平台比较时必须标注口径。

## 典型调用

```python
metrics = AbuMetricsBase(*result)
metrics.fit_metrics()
metrics.plot_returns_cmp()
```

## 参数搜索和验证边界

`GridSearch` 枚举因子参数并使用评分器比较结果。`AbuCrossVal` 根据相关性范围随机抽取股票池，它不是按时间滚动的机器学习交叉验证。开发新验证流程时应明确训练期、选择期和最终评估期。

## 扩展规则

- 新指标放在指标类中，并为无订单、短区间和零波动定义行为。
- 新评分器继承 `AbuBaseScorer`，说明每个指标是越大越好还是越小越好。
- 指标字段改名时同步 WidgetBu、结果导出和教程。
- 参数搜索必须保存完整因子配置、数据区间和随机种子，避免只保留最高分。

## 相关模块

[TradeBu](../TradeBu/README.md) 提供结果对象，[AlphaBu](../AlphaBu/README.md) 被验证流程调用，[SimilarBu](../SimilarBu/README.md) 提供相关性分组。
