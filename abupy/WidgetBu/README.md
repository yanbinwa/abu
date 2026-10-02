# WidgetBu 模块开发说明

WidgetBu 使用 IPython 和 ipywidgets 将回测、行情更新、策略配置及分析工具组合为 Notebook 界面。它是表现和编排层，核心计算仍由其他业务模块完成。

## 界面索引

| 入口 | 作用 |
| --- | --- |
| `WidgetRunLoopBack` | 配置并执行历史回测 |
| `WidgetUpdate` | 下载和更新行情 |
| `WidgetGridSearch` | 搜索策略参数组合 |
| `WidgetCrossVal` | 相关性股票池验证 |
| `WidgetVerifyTool` | 策略验证工具集合 |
| `WidgetQuantTool` | 技术线、相关性和数据分析集合 |
| `WidgetStockInfo` | 股票基础信息展示 |
| `WidgetUmp` | UMP 训练和预测配置 |

买入、卖出、选股和仓位控件分别由 `ABuWGBuyFactor`、`ABuWGSellFactor`、`ABuWGPickStock` 和 `ABuWGPosition` 实现，对应的 Manager 负责收集配置字典。

## 运行环境

项目已配置 `ABU (Python 3.11)` Jupyter 内核。界面依赖 IPython、ipywidgets 7、Notebook 6 和前端 widgets 扩展。建议从 `abupy_ui` 目录启动 Notebook，以保持旧入口的相对导入路径。

## 设计规则

- 控件只负责收集参数、调用业务接口和展示结果，不复制策略算法。
- 因子控件输出必须与高层回测入口的配置字典格式一致。
- 长任务应显示进度并恢复按钮状态，异常信息不得被清屏逻辑吞掉。
- 新控件需要验证初始值、禁用状态、重复点击和无数据结果。
- Notebook 前端兼容问题应与核心计算问题分开定位。

## 新因子界面接入

先完成并验证因子类，再继承对应 Widget 基类定义参数控件，最后在 Manager 中注册。参数名称应与因子构造参数完全一致，并提供合理默认值和说明。

## 相关模块

[AlphaBu](../AlphaBu/README.md) 和 [CoreBu](../CoreBu/README.md) 提供执行入口，[MetricsBu](../MetricsBu/README.md) 提供结果，[UmpBu](../UmpBu/README.md) 提供交易过滤。
