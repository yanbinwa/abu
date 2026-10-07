# Alpha158 ML M6 正式历史筛选验证

- 状态：`COMPLETE_REJECTED`
- 区间：2023-07-27 至 2026-09-30，772 个交易日，13 折，1,981,817 条 OOS 预测。
- 预测覆盖：与冻结 Ridge 键完全相等，无重复；最后一折 43,166 条预测全部保留，其中成熟诊断标签为 0，证明交易预测未按未来标签删行。
- 正式输出：`/Users/wjy/abu/backtests/alpha158_ml_factor_optimization_v1_final_20261007`
- 独立重放：`/Users/wjy/abu/backtests/alpha158_ml_factor_optimization_v1_r2_20261007` 与 `r3_20261007`

## 结果

| 模型 | 25bp累计收益 | 25bp年化 | 最大回撤 | 相对Ridge年化增量 | 状态 |
|---|---:|---:|---:|---:|---|
| Ridge | 5.94% | 1.90% | -7.23% | — | 基线复现 |
| ElasticNet | 8.32% | 2.64% | -5.46% | +0.74pp | `REJECT_HISTORICAL_SCREEN` |
| LambdaRank | 6.50% | 2.08% | -4.97% | +0.18pp | `REJECT_HISTORICAL_SCREEN` |

ElasticNet 的主要失败项为 ES95 变差、20 日配对区块年化增量区间下界小于零、仅两个年度胜出，且 40bp 平均仓位差超过 1pp。LambdaRank 的主要失败项为年化增量不足 0.50pp、ES95 变差、区间下界小于零、仅一个年度胜出，且三档成本仓位差均超过 1pp。

## 可重复性

- `oos_predictions.csv.gz` SHA256：`462be95659457f6f41a4e72a7c818d84e466b62030f96bc2ea32d0b5869eab86`
- `fold_manifests.json` SHA256：`4f151dacfb0d712a933e08cd1db56e224e263ae435a4f4640f67b91be745e646`
- `evidence.json` SHA256：`199fb4f757c20271a61e0d744c2198b455c415ded628985ead96b2f09749b992`
- 最终 `report.json` SHA256：`23861e94ff48ca046a1ba25d9de72326643937925c6228b7f5529b5a041bd9d6`
- 两次重放的上述文件、36 个账户核心文件、指标与决定均逐字节一致。

## 结论边界

本阶段证明候选在冻结历史上的表现和失败原因可复现，不构成新留出证据。不得扩大网格、修改标签或从两个失败模型中择优接入 shadow。
