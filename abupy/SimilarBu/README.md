# SimilarBu 模块开发说明

SimilarBu 负责相关系数计算、相似标的排序、缓存和对比绘图。它被选股、技术线和策略验证模块使用，核心前提是不同标的价格序列已经按日期对齐。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuCorrcoef.py` | Pearson、Spearman、Sign 和滚动相关计算 |
| `ABuSimilar.py` | 单标的相似度查询和多市场相关矩阵 |
| `ABuSimilarCache.py` | 相似度结果 HDF5 缓存 |
| `ABuSimilarDrawing.py` | 多标的归一化价格对比图 |

## 主要入口

- `find_similar_with_se` 按明确日期范围搜索相似标的。
- `find_similar_with_folds` 按历史年数搜索。
- `find_similar_with_cnt` 按数据数量搜索。
- `corr_xy` 和 `corr_matrix` 计算序列或矩阵相关性。
- `multi_corr_df` 比较多种相关系数结果。

## 关键设置

`g_rolling_corr_window` 默认为 60 个数据点。`g_process_panel_cnt` 根据 CPU 数量决定并行分块。大市场计算前应控制内存和进程数。

## 使用边界

相关性只描述样本区间内的共同变化，不代表因果关系或未来稳定性。比较前应统一复权、币种、频率和交易日，并明确使用价格、收益率还是涨跌幅。

## 扩展规则

- 新相关系数加入 `ECoreCorrType` 并在统一入口分派。
- 缓存键必须包含市场、区间、频率和算法，避免复用错误结果。
- 遇到常数序列、缺失值和样本不足时返回明确结果。
- 排名结果应保持稳定排序，便于实验复现。

## 相关模块

[MarketBu](../MarketBu/README.md) 提供对齐行情，[PickStockBu](../PickStockBu/README.md) 使用相似排名，[MetricsBu](../MetricsBu/README.md) 用相关区间构造验证股票池。
