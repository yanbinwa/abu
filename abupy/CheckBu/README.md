# CheckBu 模块开发说明

CheckBu 为函数参数和返回值提供声明式检查及预处理。它通过装饰器绑定函数签名，适合在公共接口边界验证类型、范围和集合关系。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuChecker.py` | 函数、参数和返回值检查器 |
| `ABuChecks.py` | 类型、边界和子集检查函数 |
| `ABuFuncUtil.py` | 函数签名、默认值和参数绑定 |
| `ABuProcessor.py` | 参数与返回值预处理装饰器 |

## 公开接口

`FuncChecker`、`ArgChecker` 和 `ReturnChecker` 组合具体规则；`arg_process` 与 `return_process` 在调用前后转换数据。`CheckError` 表示检查失败。

## 使用边界

- 检查规则应快速且无副作用，不在装饰器内读取行情或修改全局状态。
- 错误信息需要包含参数名、期望条件和实际值类型。
- 装饰器会影响函数签名和调试堆栈，新增规则时验证 Notebook 帮助信息。
- Python 版本升级时重点检查 `inspect` 签名兼容路径。

## 扩展规则

通用规则放入 `ABuChecks.py`，函数绑定逻辑保留在 `ABuFuncUtil.py`。新增处理器需要说明是在验证前还是验证后执行，并保持原函数元数据。

## 相关模块

[CoreBu](../CoreBu/README.md) 提供签名兼容能力。其他模块可在对外入口使用 CheckBu，但内部数值循环应避免增加不必要开销。
