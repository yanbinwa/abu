# M1 涨跌停参考价来源审计

| 字段 | 结论 |
|---|---|
| 审计日期 | 2026-10-03 |
| 本地未复权研究行情 | 无 `pre_close/reference_price`，无直接涨停价/跌停价 |
| ABU 行情缓存 | 有 `pre_close`，但它由适配器对前复权收盘 `shift(1)` 生成 |
| Sina/Tencent 历史接口 | 当前标准化 schema 只有未复权 OHLCV 等字段，无显式参考价 |
| Eastmoney 历史接口 | 返回涨跌额和涨跌幅，但不是显式参考价；本次在线抽查连接失败，语义未验证 |
| 当前行情快照 | 可提供“昨收”，只能从开始每日留档后向前积累，不能历史回补 |
| 最终结论 | 当前本地数据不能批量生成权威历史涨跌停事实 |

## 1. 代码审计

`ABuDataFeedAkShare._normalize_kline` 先计算 `previous_close = close.shift(1)`，随后写入 `pre_close`。当前全市场缓存使用 `qfq`，因此该字段是前复权价格序列的前一根收盘，不是交易所当日除权除息参考价。

`selection_research/raw/*.csv` 使用未复权 Sina/Tencent 历史行情。其 schema 为：

```text
date, open, high, low, close, volume, amount,
outstanding_share, turnover
```

它可以作为 `previous_raw_close` 的对账来源，但缺少参考价来源证明，不能自动升级为 `limit_reference_price_raw`。

## 2. 候选来源审计

| 来源 | 历史范围 | 明确昨收 | 直接上下限 | 当前决定 |
|---|---|---:|---:|---|
| AKShare Sina 历史 | 长历史 | 否 | 否 | 只保留 OHLCV |
| AKShare Tencent 历史 | 长历史 | 否 | 否 | 只保留 OHLCV |
| AKShare Eastmoney 历史 | 长历史 | 否 | 否 | `涨跌额/涨跌幅` 语义验证前不使用 |
| AKShare 当前快照 | 当日 | 是 | 接口字段依来源变化 | 每日不可变快照向前积累 |
| 交易所参考文件/行情主站 | 依文件 | 目标来源 | 目标来源 | 后续采集优先级最高 |
| 公司行为重建 | 已收集部分历史 | 公式重建 | 否 | 仅在行动和特殊状态完整时使用 |

本次无法连接 Eastmoney 历史端点，因此没有把候选接口的 schema 能力写成已验证覆盖。失败本身不改变本地覆盖结论。

## 3. 官方规则核验

状态机 v2 依据沪深交易所公开规则冻结以下制度：

- 科创板为 20%，IPO 前五个交易日无日涨跌幅限制；
- 创业板自 2020-08-24 起为 20%，注册制 IPO 前五个交易日无日涨跌幅限制；
- 主板注册制新股自 2023-04-10 起前五个交易日无日涨跌幅限制；此前上市首日采用 44%/36% 旧规则；
- 退市整理首日和重新上市首日无日涨跌幅限制；
- 主板风险警示股票在 2026-07-06 前为 5%，当日起为 10%；
- 科创板、创业板风险警示股票继续适用各自板块 20% 比例。

核验来源为上交所、深交所 2020、2023 和 2026 年交易规则及官方投教材料。实现将规则版本固定为 `cn_equity_limit_v2_20260706`。

## 4. 工程决定

1. 新增独立 `LimitReference` sidecar，不改写行情缓存。
2. 处理优先级固定为直接交易所上下限、明确 provider `pre_close`、完整公司行为重建、UNKNOWN。
3. `previous_raw_close` 只能对账；没有来源证据时不得自动成为事实参考价。
4. 执行层遇到 UNKNOWN 可使用带原因码的保守旧规则 fallback；事实层仍保持 UNKNOWN。
5. 从模拟盘启用日起保存当日参考价原始快照，逐日形成可验证历史。

覆盖明细由 `scripts/audit_limit_reference.py` 生成到：

```text
/Users/wjy/abu/data/selection_research/limit_reference/
```

