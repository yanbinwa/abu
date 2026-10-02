# Selection Research v2 — Milestone M0 Review

| 字段 | 内容 |
|---|---|
| 里程碑 | M0：冻结基线与数据快照 |
| 状态 | Pass |
| 完成日期 | 2026-10-02 |
| 基线代码 | `460ef76` |
| Spec 版本 | `0.1.0` |
| Snapshot ID | `sha256:41e541057ccce0d0a136420912cd9efdc15ac5d5f4638420529f08f27151ad9c` |

## 完成内容

- 新增稳定文件哈希和快照清单工具 `freeze_selection_snapshot.py`；
- 新增真实字段覆盖审计工具 `audit_selection_coverage.py`；
- 冻结10,906个信号和研究数据文件；
- 登记两个旧回测目录共106个结果文件；
- 将 placebo v1 标记为 `invalid_for_inference`；
- 增加4项快照与覆盖审计测试。

## 真实数据审计

| 指标 | 结果 |
|---|---:|
| 证券数 | 5,448 |
| 原始行情文件 | 5,448 |
| 信号行情文件 | 5,448 |
| 总行数 | 7,781,161 |
| 日期范围 | 2020-01-02 至 2026-09-30 |
| OHLCV 覆盖率 | 100% |
| 成交额覆盖率 | 97.6037% |
| 流通股本覆盖率 | 97.6037% |
| 换手率覆盖率 | 97.6037% |
| 完整注意力字段证券 | 5,223 |
| 注意力字段不完整证券 | 225 |
| 有行业历史证券 | 5,224 |
| 有深市简称变更历史证券 | 3,021 |
| 有公司行为记录证券 | 5,413 |

明确记录的52个腾讯回退文件不能代表完整缺失范围；直接扫描发现225只证券缺少至少一个注意力字段。后续共同样本消融以实际字段覆盖为准。

## 生成产物

- `/Users/wjy/abu/data/selection_research/snapshot_manifest.json`
- `/Users/wjy/abu/data/selection_research/baseline_registry.json`
- `/Users/wjy/abu/data/selection_research/coverage_daily.csv`
- `/Users/wjy/abu/data/selection_research/coverage_symbol.csv`
- `/Users/wjy/abu/data/selection_research/provider_provenance.csv`
- `/Users/wjy/abu/data/selection_research/coverage_summary.json`

## 自测结果

- M0 针对性单元测试：4项通过；
- 仓库全套 Python 单元测试：16项通过；
- Python `compileall`：通过；
- `git diff --check`：通过；
- 重新扫描10,906个输入文件后 Snapshot ID 一致；
- 旧结果登记状态和106个文件哈希验证通过。

## 已知限制

- 缓存文件过去的实际供应商没有完整保存，只能以字段特征标为 `sina_like` 或 `tencent_like_or_incomplete`；
- 上海历史 ST 区间仍不完整；
- M0 只冻结和审计数据，不修复证券生命周期或成交模型。

## 决策

**Go**：M0 达到完成条件，可以进入 M1。M1 必须使用已冻结的 Snapshot ID，不得在加载过程中更新行情或研究元数据。
