# MLBu 模块开发说明

MLBu 是面向研究的通用机器学习封装层。它把特征表、目标值、估计器创建、训练、预测、交叉验证和可视化组织在一起，也为 UmpBu 提供模型与分析能力。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuML.py` | `AbuML` 主封装、任务类型识别、训练和预测 |
| `ABuMLCreater.py` | 分类、回归、聚类和集成模型工厂 |
| `ABuMLExecute.py` | 交叉验证、学习曲线、ROC、混淆矩阵和边界图 |
| `ABuMLGrid.py` | 常用超参数网格搜索 |
| `ABuMLPd.py` | DataFrame 业务层及价格、BTC 示例 |
| `ABuMLBinsCs.py` | 订单特征分箱可视化 |

## 支持的任务

`EMLFitType` 区分分类、回归、聚类、降维和自动判断。模型工厂包含线性模型、决策树、随机森林、Boosting、Bagging、KNN、SVM、KMeans、PCA 和高斯混合模型。

部分方法名称保留了早期 scikit-learn 术语。升级依赖时应先运行实际路径，不能只依赖导入成功。

## 使用原则

- 输入 `x`、`y` 和 `df` 的行必须严格对齐。
- 预处理只在训练集拟合，再应用于验证集和测试集。
- 时间序列任务不得随机打乱后把未来样本泄漏到训练集。
- 分类阈值搜索需要同时报告准确率、覆盖率和类别分布。
- 模型图形是诊断工具，不替代样本外收益和风险验证。

## 扩展方式

通用估计器在 `AbuMLCreater` 中创建；新的评估图放入 `ABuMLExecute`；量化业务特征应优先在 TradeBu 的特征系统实现，再由 UmpBu 或研究代码消费。新增估计器需遵循 scikit-learn 的 `fit`、`predict` 约定，并说明支持的任务类型。

## 兼容注意

部分教学路径仍包含 pandas `as_matrix` 和早期 NumPy 别名。当前核心包可以在 Python 3.11 导入，但未逐个验证这些教学方法。修改时应使用 `to_numpy` 和明确的数据类型替换旧接口。

## 相关模块

[TradeBu](../TradeBu/README.md) 生成交易特征，[UmpBu](../UmpBu/README.md) 使用模型过滤信号，[MetricsBu](../MetricsBu/README.md) 评估最终策略表现。
