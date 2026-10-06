# eltdx 短线事件数据源只读探测（2026-10-06）

## 结论

`eltdx 3.2.3` 可以作为短线事件前瞻采集的候选主源，AKShare/东方财富继续
作为第二来源。当前只完成只读探测和审计工具升级，尚未修改正式前瞻采集器、
shadow 准入规则、模拟盘订单或风险状态。

## 环境与能力

- 环境：Apple Silicon、Python 3.11.17；
- 安装：官方 macOS ARM64 wheel，无需本地编译；
- 调用：`F10Client.limit_up_down_list()`；
- 可用字段：涨停、炸板、跌停、实际板位、市场最高板、原因、补充原因、
  封单额、触板时间、开板次数和行业；
- 热点题材接口可以返回题材 ID、名称、关联度、入选日期和入选原因；
- 初次连续调用发生过一次超时，降低频率并重试后成功，因此必须保留超时、
  指数退避和第二来源对账，不能将其视为无故障数据服务。

## 历史深度

| 查询日 | eltdx | AKShare |
|---|---:|---:|
| 2020-01-02 | 94 条事件 | 涨停池为空；炸板/跌停超出保留期 |
| 2026-09-30 | 75 条事件 | 73 条去重事件 |

`eltdx` 能返回 2020 年事件，但这些记录在 2026 年才被查询，统一标记为
`BACKFILLED_QUERY` 和 `asof_feature_allowed=false`。历史简称、原因和题材可能
经过修订，因此可用于覆盖研究、标签核验和候选假设，不得冒充严格历史 PIT
信号。

## 2026-09-30 双源对账

| 状态 | eltdx | AKShare | 交集 | Jaccard |
|---|---:|---:|---:|---:|
| 涨停 | 52 | 52 | 52 | 1.0000 |
| 炸板 | 14 | 12 | 12 | 0.8571 |
| 跌停 | 9 | 9 | 9 | 1.0000 |

炸板差异为 `600383`、`601238`，两只仅出现在 eltdx。差异应保留为审计事实，
不得自动选择数量更多的一方，也不得静默做并集后称为权威真值。

## 推荐接入顺序

1. 将 eltdx 作为独立 shadow sidecar 接入收盘采集，但暂不加入前瞻锚点的
   必需数据集；
2. 每日同时保存 eltdx 与 AKShare 原始响应、规范化结果和集合差异；
3. 连续真实交易日验证超时率、字段漂移、空响应和双源差异；
4. 只有事件事实稳定后，才允许将 eltdx 升为必需主源；
5. 热点题材只能从首次前瞻归档日起使用。即使记录含较早的入选日期，也不能
   据此回填历史可得时间；
6. 龙头和超预期特征继续由内部基于冻结事件事实计算，不直接采用供应商综合分。

## 审计产物

- `/Users/wjy/abu/data/selection_research/shortline_source_audit_eltdx_20261006_v1/source_audit.json`
- `/Users/wjy/abu/data/selection_research/shortline_source_audit_eltdx_20261006_v1/source_coverage_matrix.csv`

审计脚本的 eltdx 查询必须显式使用 `--enable-eltdx`。依赖缺失、请求失败和空
响应均失败关闭；回补查询不会被标记为 as-of 合格样本。

## 2026-10-06 侧车落地结果

上述第 1—3 项已按保守边界实现：

- `collect_shortline_events.py --enable-eltdx-shadow` 会追加不可变的
  `eltdx_limit_up_down_list` 原始批次和规范化结果；
- `run_vcp_paper_pipeline.py` 默认启用该侧车；
- 运行记录保存 `cross_source_comparisons`，但不判断哪一侧为真；
- eltdx 不在 `shortline_forward_v1.required_datasets` 中，始终
  `strategy_feature_allowed=false`，失败不改变五个 AKShare 数据集的归档判定；
- 原因字段保存为 `limit_reason_raw`，没有映射为 `selection_reason_raw`。

使用冻结依赖对 2026-09-30 做真实回放，eltdx 75 条记录通过 schema 校验，
双源差异仍为涨停、跌停完全一致，炸板多出 `600383`、`601238`。由于采集日为
2026-10-06，全部批次均正确标记为 `BACKFILLED_QUERY`，未获得前瞻资格。

自动化测试覆盖侧车正常、侧车超时、原因字段隔离、双源差异和主锚点不受影响。
在积累连续的同日 15:20 后样本前，不得将该来源升级为策略字段或必需主源。
