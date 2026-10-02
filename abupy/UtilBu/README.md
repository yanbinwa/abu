# UtilBu 模块开发说明

UtilBu 提供跨模块复用的日期、文件、统计、回归、缩放、字符串、平台和进度工具。业务模块应复用这些公共实现，但不应把交易规则下沉到工具层。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuDateUtil.py` | 日期格式、日期差、星期和时区 |
| `ABuFileUtil.py` | CSV、HDF5、pickle 和普通文件操作 |
| `ABuRegUtil.py` | 线性及多项式回归、误差和阶数选择 |
| `ABuStatsUtil.py` | 距离、矩、正态性和描述统计 |
| `ABuScalerUtil.py` | 对数、最小最大、标准化和序列缩放 |
| `ABuKLUtil.py` | 标准 K 线的星期、涨跌和波动统计 |
| `ABuProgress.py` | 单进程、多进程和 Notebook 进度显示 |
| `ABuDTUtil.py` | 装饰器、异常包装和绘图上下文 |
| `ABuStrUtil.py` | 编码、字符串判断和随机标识 |
| `ABuPlatform.py` | 平台与依赖版本信息 |
| `ABuOsUtil.py` | macOS 与 Windows 平台能力代理 |

## 使用约定

- 日期入口应先通过 `fix_date` 或 `fmt_date` 规范化，再进入市场模块。
- 文件函数会创建目录或覆盖目标，调用方负责确认路径和生命周期。
- 回归与统计函数通常接收 NumPy 或 pandas 对象，调用前保持索引对齐。
- 缩放函数用于可视化时不能反向解释为原始收益。
- 进度工具在子进程和 Notebook 中有不同通信路径，异常时先关闭 UI 进度复现。

## 关键设置

`ABuProgress.g_show_ui_progress` 控制 Notebook 进度；`ABuStatsUtil.g_euclidean_safe` 控制欧氏距离的安全处理。平台差异应通过 `ABuOsUtil` 和 `ABuPlatform` 判断，避免在业务模块散布系统分支。

## 扩展规则

- 新工具应保持无业务状态，输入输出和异常语义清楚。
- 文件写入函数必须明确是否创建、覆盖或删除数据。
- 数值函数应处理空数组、NaN、无穷值和常数序列。
- 对外复用的工具需从 `UtilBu.__init__` 导出并补充本文件索引。

## 相关模块

[CoreBu](../CoreBu/README.md) 提供全局环境和兼容层，[MarketBu](../MarketBu/README.md) 定义标准 K 线格式。
