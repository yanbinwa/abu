# 短线事件前瞻采集 v1 运行手册

## 1. 目标与边界

本流程只为前瞻研究保存当时可见的原始快照。数据先进入 `shadow_only`，不会改变 `vcp_residual_v2` 的选股、风控、成交或企业微信通知。

当前 AKShare/东方财富接口可提供涨停、跌停、炸板、昨日涨停和强势股池。现有接口没有经过验证的全市场竞价明细和题材原因历史流，因此：

- 09:26 只保存全市场报价代理，并标记 `PROXY_NOT_EXACT_AUCTION_FEED`；
- `所属行业` 保存为 `source_category_raw`，不能当作题材；
- `入选理由` 只作为供应商原文，不能替代涨停原因；
- 题材、原因和精确竞价因子在接入带时间戳的来源前保持 `UNKNOWN`。

## 2. 手工运行命令

交易日 09:26—09:35：

```bash
.venv/bin/python scripts/collect_shortline_events.py --phase auction
```

交易日 15:20 后：

```bash
.venv/bin/python scripts/run_vcp_paper_pipeline.py
```

流水线顺序为：行情与参考价快照、短线事件 shadow 快照、冻结的模拟盘策略、企业微信成交提醒。短线事件采集失败会记录 `shadow_error`，不会阻断冻结策略。

如 15:20 前已运行行情更新，15:20 后再次运行即可补抓当天事件。非交易日不会写入零事件事实，也不会回补上一交易日。

## 3. 数据目录

```text
/Users/wjy/abu/data/selection_research/shortline_forward/
  YYYYMMDD/
    dataset/
      batch_id/
        provider_frame.json
        normalized.csv        # 首个合格 payload 才生成
        metadata.json
  _runs/YYYYMMDD/run_id.json
```

每次请求都有独立 `batch_id`。同一 payload 再次出现时保留新的原始响应，但 `metadata.json` 的 `normalized_path` 指向首次规范化结果。

15:05 行情原始响应保存在：

```text
/Users/wjy/abu/paper/vcp_residual_v2/market_snapshots/YYYYMMDD/raw_batches/
```

固定的 `stock_spot.csv` 和 `limit_reference.csv` 是首个合格快照，内容冲突时失败，不覆盖。

## 4. 日常检查

```bash
.venv/bin/python scripts/audit_shortline_forward.py
```

检查项：

- 原始 payload 与元数据哈希一致；
- 规范化路径存在；
- 只有 `FORWARD_CAPTURE + success + schema 完整` 可进入 as-of shadow；
- 空响应、schema 漂移和请求失败没有被记成零事件。

连续采集验收以 `eligible_session_count` 为准。非交易日产生的 `_runs` 记录不计为有效会话。

## 5. 失败和补抓

1. 查看对应 `_runs/YYYYMMDD/*.json` 的 `status_counts`。
2. `empty_ambiguous`：保留原批次，稍后重新执行；不得手工改成空事件日。
3. `schema_error`：冻结旧 adapter，更新字段映射和测试后再抓；旧原始响应不得改写。
4. `SOURCE_REQUEST_FAILED`：按接口保留期内重试；补抓保留真实 `ingested_at`，自动标为 `BACKFILLED_QUERY`，不能进入 as-of 特征。
5. 行情固定快照冲突：停止模拟盘推进，比较两个原始 batch，不能覆盖首个快照。

## 6. 题材 taxonomy

输入 CSV 必须包含：

```text
source,source_theme_id,source_theme_name,canonical_theme_id,
canonical_theme_name,taxonomy_version,valid_from,valid_to,
mapping_available_at,relation
```

发布命令：

```bash
.venv/bin/python scripts/import_theme_taxonomy.py mapping.csv
```

同一快照不可覆盖。策略查询同时满足业务有效区间和 `mapping_available_at <= decision_at`；忽略该限制的结果只能命名为 `retrospective`。

## 7. 时间与节假日

- 所有运行时点使用 `Asia/Shanghai` 且必须带时区；
- 交易日使用 AKShare 的交易日历校验；
- 周末和节假日只写运行证据，不写正常零事件；
- 系统时钟漂移、交易日历不可用或采集提前时，停止对应数据批次，不推断结果。
