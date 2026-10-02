# CoreBu 模块开发说明

CoreBu 是系统入口和运行环境层。它定义全局配置、统一第三方库差异、调度并行任务，并负责保存回测结果。其他业务模块通常都直接或间接依赖它，因此修改这里需要优先评估全局影响。

## 主要职责

- `ABu.py` 提供行情更新、选股择时回测和结果存取的高层入口。
- `ABuEnv.py` 定义市场、数据源、缓存、目录、特征采集和 UMP 开关。
- `ABuStore.py` 定义 `AbuResultTuple`，保存订单、行为、资金和基准对象。
- `ABuParallel.py` 与 `ABuEnvProcess.py` 统一多进程、多线程及环境传播。
- `ABuFixes.py` 与 `ABuPdHelper.py` 隔离 Python 和科学计算库的版本差异。
- `ABuBase.py` 提供参数对象、冻结属性和序列化混入类。

## 公开入口

| 接口 | 用途 |
| --- | --- |
| `abu.run_loop_back` | 执行选股和择时回测，返回 `AbuResultTuple` 与 `AbuKLManager` |
| `abu.run_kl_update` | 批量更新目标市场行情，并切换为本地读取模式 |
| `abu.store_abu_result_tuple` | 保存回测结果 |
| `abu.load_abu_result_tuple` | 读取回测结果 |
| `env.enable_example_env_ipython` | 使用 `RomDataBu` 的内置历史数据 |
| `env.disable_example_env_ipython` | 返回普通数据获取模式 |

## 关键设置

| 设置 | 默认值 | 影响 |
| --- | --- | --- |
| `g_market_target` | 美股 | 标的集合、基准和市场规则 |
| `g_market_source` | 百度源 | 默认在线行情适配器 |
| `g_data_fetch_mode` | 普通模式 | 本地优先、强制本地或强制网络 |
| `g_data_cache_type` | CSV | 行情缓存格式 |
| `g_market_trade_year` | 随初始化市场设置 | 年化指标和特征窗口 |
| `g_enable_ml_feature` | `False` | 是否在订单上生成机器学习特征 |
| `g_enable_take_kl_snapshot` | `False` | 是否保存 K 线特征快照 |
| `g_enable_train_test_split` | `False` | 是否划分训练与测试股票池 |
| 各 UMP 开关 | `False` | 是否启用内置主裁和边裁 |

运行数据默认写入 `~/abu`。直接修改 `g_market_target` 后，依赖市场的派生值不会全部自动刷新，新增入口应显式处理这类配置一致性。

## 回测调用链

`run_loop_back` 依次创建 `AbuBenchmark` 和 `AbuCapital`，调用 `AbuPickStockMaster` 筛选股票池，通过 `AbuKLManager` 准备行情，再由 `AbuPickTimeMaster` 生成订单和交易行为。资金执行完成后，调用方使用 MetricsBu 计算绩效。

## 扩展和修改规则

- 新增全局配置时，确认它是否需要被 `AbuEnvProcess` 复制到子进程。
- 新增结果字段时，同步检查 `AbuResultTuple`、HDF5 存取、导出和指标模块。
- 兼容适配应集中放在 `ABuFixes.py` 或 `ABuPdHelper.py`，避免业务模块重复判断版本。
- `gen_buy_from_chinese` 当前没有策略生成实现，不应作为可用入口依赖。

## 相关模块

[MarketBu](../MarketBu/README.md) 提供行情，[AlphaBu](../AlphaBu/README.md) 执行策略，[TradeBu](../TradeBu/README.md) 管理交易对象，[MetricsBu](../MetricsBu/README.md) 评估结果。
