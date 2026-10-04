# Alpha158 多因子 v4 M0 冻结与预登记审查

## 结论

M0 的冻结对象、已观察历史和实验顺序已经登记。该阶段只建立证据边界，没有运行 v4 收益实验，也没有产生实盘准入结论。

当前状态：`M0_VERIFIED / RESEARCH_ONLY`。

## 冻结基线

- 实施起点：`a346527871a802c4f238c47ed7a49b7918864a5a`，`main` 与 `origin/main` 一致；
- 实施开始时 v4 Spec 和开发计划为未跟踪文件，已如实记录在基线清单；
- v1、v2、v3 配置同时保存文件哈希和规范化配置哈希；
- 历史研究报告、预测、结果、净值、订单、成交、风险决策和试验登记保存内容哈希；
- v2 和 v3 明确标记为 `observed_post_hoc_diagnostic`；
- 2022-01-01 至 2026-10-03 的历史只能用于开发和诊断，不是独立留出样本。

冻结清单：

- `configs/selection/alpha158_multifactor_baseline_v4.json`
- `configs/selection/alpha158_multifactor_experiments_v4.json`

v4 独立输出根目录为 `/Users/wjy/abu/backtests/alpha158_multifactor_v4`，不与三个冻结历史目录重叠。

## 预登记顺序

实验顺序固定为 `D0 → D1 → B2 → B3 → B4 → S1 → B5 → F1`。每个阶段登记唯一变化、继续门禁、失败状态和独立输出目录。任何前置阶段失败都不得通过增加模型复杂度绕过。

## 已知缺口

1. 免费基本面数据源尚未通过来源、公告日、修订历史、科目和单位验收；
2. 上海历史名称/ST 覆盖仍可能不完整，达到 M4 前不得启动全市场主实验；
3. 当前 `market_cap = outstanding_share × raw_close` 是流通市值，不能用于 E/P、CFO/P 等总市值分母；
4. 当前 Alpha158-lite 模型分数是横截面排序，不是可以直接减交易成本的收益百分点；
5. 当前历史已经反复观察，只能建立滚动诊断证据，不能重新包装成全新留出样本；
6. 系统 `/usr/bin/python3` 缺少项目依赖，正式自测必须使用仓库 `.venv/bin/python`。

## M0 验收方法

只读校验命令：

```bash
.venv/bin/python scripts/freeze_multifactor_snapshot_v4.py
.venv/bin/python -m unittest tests.test_multifactor_v4 -v
.venv/bin/python -m compileall -q abupy scripts tests
git diff --check
```

校验器会在配置、实验目录、历史报告或外部历史账本任何一个字节变化时失败。便携 CI 可以使用 `--skip-external` 只核对版本库内文件，但 M0 本机验收必须校验外部冻结账本。
