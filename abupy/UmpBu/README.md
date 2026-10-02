# UmpBu 模块开发说明

UmpBu 是交易信号过滤层。它使用历史订单特征训练模型，在买入订单成立前判断是否拦截。UMP 不生成买卖信号，也不保证提升收益；它依赖训练数据、特征口径和样本外验证。

## 主裁和边裁

主裁 `AbuUmpMainBase` 对特征组合进行分类和命中判断，主要目标是识别应阻止的交易。边裁 `AbuUmpEdgeBase` 评估交易的收益边界和风险。`AbuUmpManager` 挂在买入因子上，统一调用已启用的内置及用户模型。

## 内置模型

| 类型 | 主裁 | 边裁 |
| --- | --- | --- |
| 走势角度 | `AbuUmpMainDeg` | `AbuUmpEdgeDeg` |
| 价格位置 | `AbuUmpMainPrice` | `AbuUmpEdgePrice` |
| 波动特征 | `AbuUmpMainWave` | `AbuUmpEdgeWave` |
| 多特征组合 | `AbuUmpMainMul`、`AbuUmpMainFull` | `AbuUmpEdgeMul`、`AbuUmpEdgeFull` |
| 跳空特征 | `AbuUmpMainJump` | 无对应内置边裁 |

## 启用流程

1. 开启 `ABuEnv.g_enable_ml_feature`，在训练回测中生成订单特征。
2. 使用训练订单拟合并保存目标主裁或边裁。
3. 在独立时间和股票池上验证拦截效果。
4. 开启 `ABuEnv` 中对应的 UMP 开关。
5. 重新运行策略并比较覆盖率、收益、回撤和未拦截样本。

所有内置拦截开关默认关闭。用户 UMP 通过 `append_user_ump` 注册，并受 `g_enable_user_ump` 控制。

## 模型和缓存

模型文件和索引写入 `ABuEnv.g_project_data_dir` 或数据库目录。`CachedUmpManager` 管理预测模型缓存。模型唯一标识、市场名称和特征列必须保持一致，否则可能加载错误模型或预测失败。

## 新 UMP 实现

- 选择主裁或边裁基类，定义稳定的 `class_unique_id`。
- 明确训练列和预测列，避免训练预测特征漂移。
- 处理缺失特征、旧模型和缓存失效。
- 保存训练区间、市场、因子版本和阈值。
- 在启用拦截前报告命中数量及误杀成本。

## 相关模块

[TradeBu](../TradeBu/README.md) 生成特征，[MLBu](../MLBu/README.md) 提供模型能力，[FactorBuyBu](../FactorBuyBu/README.md) 在创建订单时调用 UMP。
