# TradeBu 模块开发说明

TradeBu 定义回测中的交易实体和资金执行规则。它连接策略信号与绩效评估，负责基准、订单、手续费、成交动作、资金时间序列和机器学习特征。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuBenchmark.py` | 确定回测时间范围、市场基准和对齐索引 |
| `ABuCapital.py` | 现金、持仓、市值和手续费时间序列 |
| `ABuCommission.py` | 各市场默认手续费函数 |
| `ABuKLManager.py` | 缓存和分配选股及择时行情 |
| `ABuOrder.py` | 订单生命周期和买卖属性 |
| `ABuTradeExecute.py` | 订单表、动作表及资金执行 |
| `ABuTradeProxy.py` | 订单集合比较、摘要和代理访问 |
| `ABuTradeDrawer.py` | 历史交易和资金曲线绘图 |
| `ABuMLFeature.py` | 订单特征生成和用户特征扩展 |

## 核心对象

`AbuBenchmark` 提供基准行情；`AbuCapital` 保存账户状态；`AbuOrder` 表示从买入到卖出的策略订单；`AbuKLManager` 为 Worker 提供行情。四者共同构成回测执行上下文。

## 订单到资金的过程

1. 因子创建或更新 `AbuOrder`。
2. `make_orders_pd` 将订单对象转换为标准表并计算单笔收益。
3. `transform_action` 拆成按日期排列的买入和卖出动作。
4. `apply_action_to_capital` 检查资金与持仓，记录 `deal`，更新账户时间序列。

策略生成的订单不一定实际成交。组合收益必须使用资金执行结果，不能只汇总订单的理论收益。

## 关键字段

订单表包含买卖日期、价格、数量、因子、方向、卖出原因、利润、结果和特征。动作表包含 `Date`、`Price`、`Cnt`、`symbol`、`Direction`、`action` 与 `deal`。修改字段时需同步 MetricsBu、UmpBu、存储和界面代码。

## 配置和扩展

- 自定义手续费通过 `AbuCapital` 的 `user_commission_dict` 传入。
- 用户交易特征通过 `append_user_feature` 注册，完成后可用 `clear_user_feature` 清理。
- 特征窗口由 `g_deg_keys`、`g_price_rank_keys`、`g_wave_xd` 和 `g_atr_xd` 等变量控制。
- K 线快照只有在 `ABuEnv.g_enable_take_kl_snapshot` 开启时生成。

新增市场时必须补齐手续费、交易单位、方向、持仓价值和基准规则。当前资金类不区分货币，跨币种组合需要另行设计。

## 相关模块

[AlphaBu](../AlphaBu/README.md) 产生订单，[BetaBu](../BetaBu/README.md) 计算仓位，[SlippageBu](../SlippageBu/README.md) 计算成交价，[MetricsBu](../MetricsBu/README.md) 消费交易结果。
