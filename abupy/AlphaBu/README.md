# AlphaBu 模块开发说明

AlphaBu 是策略执行编排层。它不定义具体交易规则，而是把股票池、K 线、买卖因子、资金和并行任务组织起来，最终生成订单表与交易行为表。

## 两阶段执行

选股阶段由 `AbuPickStockMaster` 分配任务，`AbuPickStockWorker` 加载选股期行情并依次执行 PickStockBu 因子。择时阶段由 `AbuPickTimeMaster` 分配标的，`AbuPickTimeWorker` 按交易日驱动买入和卖出因子。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuPickBase.py` | 选股与择时 Worker 的公共基础 |
| `ABuPickStockMaster.py` | 选股任务切分和并行调度 |
| `ABuPickStockWorker.py` | 单组股票池的选股执行 |
| `ABuPickStockExecute.py` | Worker 包装、进度和结果汇总 |
| `ABuPickTimeMaster.py` | 择时任务切分和并行调度 |
| `ABuPickTimeWorker.py` | 单标的逐日事件循环 |
| `ABuPickTimeExecute.py` | 多标的执行包装和错误分类 |

## 主要入口

- `AbuPickStockMaster.do_pick_stock_with_process` 返回筛选后的证券代码列表。
- `AbuPickTimeMaster.do_symbols_with_same_factors_process` 对多个标的执行同一组因子。
- `alpha.do_symbols_with_diff_factors` 支持标的使用不同因子配置。

高层调用应优先使用 `CoreBu.abu.run_loop_back`，只有需要自定义编排时才直接调用 Master。

## 配置和并行

进程数量由高层入口传入。环境配置必须通过 `ABuEnvProcess` 传播。`ABuPickTimeWorker.g_natural_long_task` 影响长任务处理方式，修改前需分别验证 macOS、Windows 和 Notebook 环境。

## 扩展和约束

- 新因子应放在 PickStockBu、FactorBuyBu 或 FactorSellBu，不应写入 Worker。
- Worker 负责时序驱动和生命周期，因子负责信号判断，资金成交由 TradeBu 处理。
- 修改结果合并逻辑时，保持订单字段、动作顺序和未平仓订单语义不变。
- 多进程问题应先用单进程复现，避免进度通信掩盖实际异常。

## 相关模块

[PickStockBu](../PickStockBu/README.md) 定义选股规则，[FactorBuyBu](../FactorBuyBu/README.md) 和 [FactorSellBu](../FactorSellBu/README.md) 定义择时规则，[TradeBu](../TradeBu/README.md) 提供资金与行情管理。
