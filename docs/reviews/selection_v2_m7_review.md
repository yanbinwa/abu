# Selection Research Engine v2 — M7 Review

## 结论

M7 的市场状态、连续路径风险、簇统计、多重检验和准入门禁已经实现。默认策略状态为 `research_only`；只有所有历史门槛同时满足时才生成前瞻登记。

## 实现

- `ABuResearchStatistics.py`
  - 使用过去252个已完成波动率观测的70%分位定义市场状态；
  - 入场日交易归因和每日 P&L 归因分开输出；
  - circular block 5/10/20 日和 stationary bootstrap 平均10日；
  - 默认每种5,000路径，计算终值、最大回撤、最长水下时间、波动、95% ES 和亏损概率；
  - 回撤修复时间包含右删失和 Kaplan–Meier 表；
  - 同日入场簇 bootstrap、前5笔盈利集中度和 BH FDR；
  - 100笔交易或40个簇/3个状态、placebo、时间块、集中度、FDR 和 liquidation NAV 联合准入。
- `run_selection_stress.py`
  - 拒绝日期重复或无序的净值路径；
  - 输出市场状态、两种归因、closed trades、四组蒙特卡洛、KM 和准入报告；
  - 蒙特卡洛明确标注只评估路径风险，不能证明 alpha；
  - 未达到门槛时只写 `forward_registration_blocked.json`。

## 自测

定向测试 `6/6` 通过：未来波动不改变历史状态、bootstrap 可复现、右删失输出、簇置信区间、BH FDR、盈利集中度、保守准入和前瞻配置哈希均正确。

真实数据开发烟雾测试使用20路径以缩短自测时间；正式默认值仍为每种5,000路径。该烟雾输入来自增加 liquidation NAV 之前保存的 A2 曲线，因此 `trend_reversal_v1` 样例得到 `research_only`，未通过项为：

- 缺少正式 placebo v2 分位；
- 不足3个独立时间块超过 placebo 中位数；
- 当前曲线没有 `liquidation_nav_3_limits`。

该结果证明门禁在证据缺失时会保守阻断，不用于评价策略最终有效性。
