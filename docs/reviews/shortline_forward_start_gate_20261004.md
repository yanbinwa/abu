# 短线事件前瞻起算闸门实施记录

日期：2026-10-04

## 冻结结论

- 名义起始日：`2026-10-09`。
- 实际起始日：该日或之后首个成功归档全部必需收盘事件集的交易日。
- 必需事件集：涨停、跌停、炸板、昨日涨停、强势股池。
- 样本模式：永久保持 `shadow_only`，本阶段不允许改变模拟盘订单。

## 工程约束

- `configs/selection/shortline_forward_v1.json` 冻结起始定义和必需数据集。
- 首个完整同日归档会创建不可变 `_forward/anchor.json`。
- 单日只有在全部必需事件集通过 schema、同日可获得性和规范化检查后，才记为
  `eligible_forward_shadow`。
- 起算日前记录为 `prestart_shadow`；起算日后但归档不完整时记录为
  `awaiting_successful_archive`。
- 采集结果必须同时声明 `feature_mode=shadow_only`、
  `order_mutation_allowed=false` 和 `paper_order_effect=none`；否则流水线拒绝该
  shadow 结果。
- 短线采集输出不作为模拟盘脚本输入，模拟盘继续使用冻结的
  `vcp_residual_v2` 独立生成订单。

## 验证

- 起算日前完整归档不会建立锚点。
- 2026-10-09 归档不完整时不会建立锚点。
- 之后首个完整归档日会建立锚点并成为第一条合格前瞻样本。
- 审计会拒绝起算日前样本、缺少完整归档证据的样本、非 shadow 运行和允许
  修改订单的运行记录。
