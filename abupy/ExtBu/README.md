# ExtBu 模块维护说明

ExtBu 保存项目内置的第三方代码和兼容实现，包括 joblib、empyrical、six、funcsigs、futures 和 OrderedDict 回退。它用于支持旧 Python 及旧依赖环境，不是业务扩展目录。

## 目录内容

| 内容 | 用途 |
| --- | --- |
| `empyrical` | 收益、风险、Alpha、Beta 和回撤计算 |
| `joblib` | 并行与序列化兼容实现 |
| `futures` | concurrent futures 回退实现 |
| `six.py` | Python 2 与 Python 3 兼容辅助 |
| `funcsigs.py` | 函数签名回退实现 |
| `odict.py` | OrderedDict 回退实现 |

## 维护原则

- 业务功能不得直接新增到 ExtBu。
- 修改前确认文件的原始许可证和上游版本，保留版权声明。
- 优先在 CoreBu 兼容层选择系统依赖或内置回退，不在业务模块直接判断。
- 替换内置副本时运行并行、序列化、指标和函数签名回归测试。
- 删除兼容代码前检查 Python 2、Windows 和历史 pickle 的兼容需求是否已经正式终止。

当前 Python 3.11 主流程仍会使用其中的 six 和 empyrical 等实现。内置版本较旧，升级时需比较指标口径，不能只以导入成功作为验收标准。

## 相关模块

[CoreBu](../CoreBu/README.md) 统一暴露兼容接口，[MetricsBu](../MetricsBu/README.md) 使用 empyrical 计算绩效。
