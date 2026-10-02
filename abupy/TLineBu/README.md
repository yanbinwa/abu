# TLineBu 模块开发说明

TLineBu 对价格序列进行趋势、通道、支撑阻力、跳空、黄金分割和波动分析。`AbuTLine` 是面向研究者的对象接口，`ABuTLExecute` 承载大部分底层计算。

## 文件索引

| 文件 | 作用 |
| --- | --- |
| `ABuTLine.py` | 技术线对象和绘图入口 |
| `ABuTLExecute.py` | 位移、速度、回归通道、骨架和支撑阻力计算 |
| `ABuTLJump.py` | 跳空缺口及权重 |
| `ABuTLGolden.py` | 黄金分割与比例位置 |
| `ABuTLAtr.py` | 序列波动标准化 |
| `ABuTLWave.py` | 波动强度和权重 |
| `ABuTLVwap.py` | 日线概念下的成交量加权价格 |
| `ABuTLSimilar.py` | 相关性、协整和相似标的应用 |

## 典型入口

`AbuTLine.show_kl_pd` 从 K 线字段建立技术线。实例可判断上升或下降趋势，绘制回归通道、骨架、支撑阻力和黄金分割。模块函数还提供 `calc_jump`、`calc_golden`、`calc_vwap` 和 `calc_pair_speed`。

## 关键设置

- `g_step_unit` 控制技术线分段基础单位。
- `g_upport_resistance_unit` 控制支撑阻力聚合粒度，变量名按源码保留拼写。
- `g_top_corr_cnt`、`g_coint_threshold` 和 `g_coint_show_max` 控制相似及协整分析。

## 扩展和验证

- 计算函数应返回数据结果，绘图留在外层接口。
- 窗口和多项式阶数必须限制，避免短序列或过拟合导致不稳定。
- 支撑阻力和形态结果是算法输出，不应在接口层包装为确定预测。
- 新方法需验证常数序列、缺失值、极短序列和异常价格。
- 涉及相似标的时，确认各序列日期已经对齐。

## 相关模块

[IndicatorBu](../IndicatorBu/README.md) 提供传统指标，[SimilarBu](../SimilarBu/README.md) 提供相关系数底层能力，[WidgetBu](../WidgetBu/README.md) 暴露交互分析界面。
