# M6 每日增量采集与题材双时态审查

> 后续证据：2026-10-06 对 `eltdx 3.2.3` 的只读实测已证明涨跌停、炸板、
> 原因和热点题材接口在当前环境可用。该证据不改变本文件当时的 M6 准入结论；
> 详见 `shortline_eltdx_source_probe_20261006.md`。

> 落地更新：eltdx 收盘事件已作为可选 shadow 侧车接入统一采集器和日终流水线，
> 同步保存双源集合差异。它仍不属于前瞻锚点必需数据集，也不具备策略资格。

## 结论

**工程实现通过，连续前瞻数据门禁未通过。**

不可变批次、失败关闭、重复 payload 复用、双时态 taxonomy、数据审计和模拟盘 shadow 接入已经完成。2026-10-03 是非交易日，真实运行正确跳过，没有把“无交易”写成“零事件”。有效交易日连续采集需从 2026-10-09 或之后首个可运行交易日开始积累。

## 代码与接口

- `ABuShortLineEvents.py`：不可变快照存储、可获得性证据、payload/schema 哈希和双时态 taxonomy；
- `collect_shortline_events.py`：09:26 报价代理、15:20 五类事件池、交易日及时点门禁；
- `audit_shortline_forward.py`：原始 payload、规范化路径和 as-of/策略资格审计；
- `import_theme_taxonomy.py`：不可覆盖 taxonomy 发布；
- `update_paper_market_data.py`：15:05 供应商原始行情逐批次留存，固定行情与参考价快照不可覆盖；
- `run_vcp_paper_pipeline.py`：事件采集作为 `shortline_shadow` 旁路运行，失败不改变冻结策略；
- `shortline_forward_capture_v1.md`：运行、补抓、schema 漂移和节假日处理流程。

## 数据和 Schema 版本

- 快照存储：`shortline_snapshot_store_v1`；
- 采集 adapter：`akshare_shortline_forward_v1`；
- 行情原始批次：`paper_market_provider_capture_v1`；
- 时区：`Asia/Shanghai`；
- 根目录：`/Users/wjy/abu/data/selection_research/shortline_forward`。

元数据同时区分：

- `asof_feature_allowed`：数据确实在决策时点可见；
- `strategy_feature_allowed`：字段语义和质量已经允许进入策略研究。

因此 09:26 全市场报价代理可以作为当时快照审计，但由于不是精确竞价源，`strategy_feature_allowed=false`。

## 测试结果

- M6、行情快照和流水线定向测试：13 项通过；
- 项目全量测试：126 项通过；
- `compileall`：通过；
- `git diff --check`：通过。

覆盖场景包括：

- 同 payload 多批次原始响应全部保留，规范化结果复用；
- schema 漂移不能进入 as-of；
- 语义代理可见但禁止进入策略；
- 非交易日不写零事件；
- 时区缺失直接失败；
- taxonomy 在 `mapping_available_at` 前不可见；
- 同日 15:20 后二次运行可补抓，周末不回补上一交易日；
- 固定行情和参考价快照发生内容冲突时拒绝覆盖。

## 覆盖、缺失和冲突

当前 AKShare/东方财富接口提供：

- 涨停池；
- 跌停池；
- 炸板池；
- 昨日涨停池；
- 强势股池。

当前缺少经过验证的：

- 全市场精确竞价明细；
- 带历史发布时间的题材成员快照；
- 完整涨停原因与变更时间流。

`所属行业` 不映射为题材，`强势股池.入选理由` 不映射为涨停原因。相关字段保持 `UNKNOWN`。

## 非交易日实跑证据

2026-10-03 运行结果：

- 状态：`skipped_non_trading_day`；
- capture 数：0；
- as-of 合格交易日：0；
- 违规：0；
- 运行证据 SHA256：`d13899e3d27359dc2541aee32ed7a7a3dea7a84870743afd74cb25f5da399248`；
- 审计报告 SHA256：`f08a67c3155b7a9bd06f410bb3e4749a8443dff3c98b1124f36617f91513560a`。

## 与旧版本差异

- 模拟盘原策略、风险预算、订单审批、成交与企业微信通知保持冻结；
- 新增事件数据只以 shadow sidecar 写入；
- 15:05 行情不再允许静默覆盖固定快照；
- 15:20 前跳过事件后，可在当日二次运行补抓；
- 回补数据保留真实 `ingested_at` 并标为 `BACKFILLED_QUERY`，不会伪装为当时可见。

## 未解决问题

1. 至少需要若干真实交易日验证接口稳定性、字段完整性和空响应语义；
2. 精确竞价、题材成员和涨停原因仍需新的数据源；
3. M1 历史参考价覆盖不足，M2—M4 仍被阻断；
4. M7 不能在没有合格题材成员和足够前瞻样本时建立正式题材强度或超预期模型。

## 是否满足完成条件

- 原始 payload 与规范化结果双向可追踪：满足；
- 重复运行幂等且原始响应不覆盖：满足；
- 当前 taxonomy 不回填历史：满足；
- 连续模拟采集通过：**未满足**；
- 精确竞价和题材原因覆盖：**未满足**。

## 下一阶段准入结论

- 允许从下一交易日开始 M6 前瞻采集；
- 允许继续冻结策略的假突破诊断；
- 不允许启用短线情绪、题材或竞价因子改变模拟盘交易；
- 不允许启动 M8B/M9B 收益检验，直至 M7 数据与预登记门禁通过。
