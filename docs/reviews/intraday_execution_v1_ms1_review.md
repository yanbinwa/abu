# 日线选股与分钟执行系统 v1 — MS1 Review

## 结果

实现标准 `MinuteBarEvent`、provider 时间语义、真实请求/接收时间、数据健康维度和空结果失败关闭。旧 DataFrame 字段继续保留，现有调用方无需立即迁移。

## 关键契约

- Eastmoney/Sina 当前 AKShare 分钟标签按 Bar 结束时间解释。
- `bar_start = bar_end - interval`。
- `available_at` 为本地首次收到响应的时间。
- 请求区间过滤后为空返回 `NO_DATA`，不记成功。
- `amount_raw` 可空并记录 `AMOUNT_MISSING`；`volume_shares` 非法则拒绝事件。
- 健康状态分离 `transport_ok/data_present/data_fresh/fields_valid`。

## 兼容性

保留 `timestamp/open/high/low/close/volume/amount/bar_complete` 等旧列，同时增加规范字段。未修改选股、执行器或纸上状态。
