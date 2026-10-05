# M7 企业微信长连接事务通知验收

日期：2026-10-05

## 结论

结论为 `WECOM_LONG_CONNECTION_NOTIFICATION_ACCEPTED`。

项目现有凭证属于企业微信智能机器人长连接模式，而不是群机器人 Webhook。本阶段新增长连接
outbox transport，使事务通知 worker 可以在不接触机器人密钥的情况下投递文字和图片，并等待
驻留机器人生成发送回执后才把分片标记为 `SENT`。

## 契约

- 分片队列 ID 由通知幂等键确定性生成；重试不会创建新的逻辑消息；
- 图片先复制为运行目录下的 SHA-256 内容寻址资产，队列不引用任意外部路径；
- 机器人读取图片前重新计算 SHA-256；不一致时拒绝发送；
- Python worker 等待 `sent-<id>.json` 回执，只有回执存在才返回成功；
- 超时进入 `UNKNOWN`，恢复后重试，不能把“已排队”冒充“已送达”；
- 旧版仅含 `content` 的文字队列继续兼容；
- 文字和图片使用独立事务分片，任一失败不会改写交易事实。

## 自测和真实验收

- Python 定向测试：9 项通过；
- Node 机器人测试：6 项通过；
- 已备份部署前的 `bot.mjs` 和 `lib.mjs`；
- LaunchAgent 重启后重新认证成功，`connected=true`、`ownerBound=true`；
- 使用独立 Mock 账户完成一天交易，成交、日结和对账通过；
- TEXT 与 CHART_IMAGE 均在第一次尝试获得长连接发送回执；
- 通知 `notification-evt-654f578d745385dfa9543d85c20a612e` 的聚合状态为 `SENT`。

本次真实发送只包含 Mock 模拟盘验收信息，不是实盘委托或投资建议。
