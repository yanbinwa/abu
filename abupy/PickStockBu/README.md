# PickStockBu 模块开发说明

PickStockBu 定义股票池过滤规则。选股因子先于择时因子运行，也可以附着在某个买入因子上，在择时过程中周期性决定该因子是否继续产生信号。

## 基类和内置因子

`AbuPickStockBase` 定义 `fit_pick` 和首次筛选扩展点。`reversed_result` 支持反转筛选结果。

| 类 | 筛选依据 |
| --- | --- |
| `AbuPickStockPriceMinMax` | 价格上下限 |
| `AbuPickRegressAngMinMax` | 价格回归角度范围 |
| `AbuPickSimilarNTop` | 与目标标的的相似度排名 |
| `AbuPickStockShiftDistance` | 位移路程比 |
| `AbuPickStockNTop` | 涨跌幅排名示例 |

## 配置格式

```python
stock_picks = [
    {'class': AbuPickRegressAngMinMax,
     'threshold_ang_min': 0.0, 'reversed': False},
]
```

`AbuPickSimilarNTop.g_pick_similar_n_top` 控制相似候选的默认数量。涉及随机抽样或排名并列时，应记录排序和随机种子规则。

## 新选股因子要求

- 继承 `AbuPickStockBase`，在 `_init_self` 中校验参数。
- 普通逐标的筛选实现 `fit_pick`；需要先观察整个股票池时实现 `fit_first_choice`。
- 对行情缺失、上市时间过短和停牌数据给出明确结果。
- 不在选股阶段修改资金或生成订单。
- 在训练测试股票池模式下验证不会读取测试集信息。

## 相关模块

[AlphaBu](../AlphaBu/README.md) 调度选股，[MarketBu](../MarketBu/README.md) 提供候选标的和行情，[SimilarBu](../SimilarBu/README.md) 支持相似度因子。
