# abupy 核心模块开发索引

本目录是 ABU 量化研究系统的 Python 实现。本文按数据、策略、交易、评估和交互界面梳理模块边界，供后续定位代码、设计改动和评审影响范围使用。当前源码版本为 0.4.0，项目验证环境为 Python 3.11。

## 主流程

```text
CoreBu 环境与入口
  └─ MarketBu 行情和证券信息
      └─ AlphaBu 选股与择时调度
          ├─ PickStockBu 选股因子
          ├─ FactorBuyBu 买入因子
          ├─ FactorSellBu 卖出因子
          ├─ BetaBu 仓位
          ├─ SlippageBu 成交价
          └─ UmpBu 交易过滤
              └─ TradeBu 订单与资金执行
                  └─ MetricsBu 绩效与验证
```

`IndicatorBu`、`TLineBu`、`SimilarBu` 和 `MLBu` 为策略与研究提供分析能力；`WidgetBu` 将主要能力组合成 Notebook 控件。

## 模块索引

| 模块 | 主要职责 | 开发文档 |
| --- | --- | --- |
| CoreBu | 全局环境、回测入口、并行与结果存储 | [CoreBu](CoreBu/README.md) |
| MarketBu | 市场标识、行情源、缓存和证券信息 | [MarketBu](MarketBu/README.md) |
| AlphaBu | 选股和择时任务调度 | [AlphaBu](AlphaBu/README.md) |
| PickStockBu | 选股因子 | [PickStockBu](PickStockBu/README.md) |
| FactorBuyBu | 买入择时因子 | [FactorBuyBu](FactorBuyBu/README.md) |
| FactorSellBu | 卖出择时因子 | [FactorSellBu](FactorSellBu/README.md) |
| BetaBu | 仓位管理 | [BetaBu](BetaBu/README.md) |
| SlippageBu | 模拟成交价格 | [SlippageBu](SlippageBu/README.md) |
| TradeBu | 基准、订单、资金、手续费和交易执行 | [TradeBu](TradeBu/README.md) |
| MetricsBu | 绩效、评分、参数搜索和交叉验证 | [MetricsBu](MetricsBu/README.md) |
| MLBu | 通用机器学习封装 | [MLBu](MLBu/README.md) |
| UmpBu | 基于交易特征的信号过滤 | [UmpBu](UmpBu/README.md) |
| IndicatorBu | MA、ATR、MACD、RSI 和布林带 | [IndicatorBu](IndicatorBu/README.md) |
| TLineBu | 趋势线、支撑阻力、跳空和波动分析 | [TLineBu](TLineBu/README.md) |
| SimilarBu | 相关系数和相似标的检索 | [SimilarBu](SimilarBu/README.md) |
| WidgetBu | Jupyter 交互界面 | [WidgetBu](WidgetBu/README.md) |
| UtilBu | 日期、文件、统计、回归和进度工具 | [UtilBu](UtilBu/README.md) |
| CheckBu | 函数参数和返回值检查 | [CheckBu](CheckBu/README.md) |
| CrawlBu | 证券元数据爬取辅助 | [CrawlBu](CrawlBu/README.md) |
| DLBu | 深度学习图片与数据集辅助 | [DLBu](DLBu/README.md) |
| ExtBu | 内置兼容与第三方辅助代码 | [ExtBu](ExtBu/README.md) |
| RomDataBu | 内置行情和市场静态数据 | [RomDataBu](RomDataBu/README.md) |

## 开发约定

- 业务模块优先从各模块的 `__init__.py` 导入公开接口。只有扩展内部实现时才直接依赖具体文件。
- 市场、缓存、特征和 UMP 开关集中在 `CoreBu.ABuEnv`。多进程任务需要通过 `ABuEnvProcess` 传播环境。
- 新策略应分别实现信号、仓位、成交和绩效验证，避免在因子中直接修改资金表。
- 回测使用日线模拟成交。新增市场前必须明确交易日数、交易单位、费用、涨跌停和保证金规则。
- 修改兼容层、行情格式、订单字段或资金执行逻辑时，应重跑内置 TSLA 回测基线。

## 当前验证基线

项目已在 Python 3.11.17 下完成核心包导入、HDF5 读写和内置 TSLA 单标的回测。该基线验证核心链路，不代表在线数据源、全部 Notebook、爬虫和每个策略均已验证。
