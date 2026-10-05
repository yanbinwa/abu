# 自闭环模拟盘 M6/M7 全链路软件验收记录

日期：2026-10-05

## 结论

结论为 `MOCK_FULL_CHAIN_ACCEPTED`。

当前代码已能在不连接券商、不读取未来数据、不调用真实企业微信的前提下，用固定 Mock 数据
跑通“订单意图 → 账户阶段 → 分钟快照 → M1 成交 → 原子账户提交 → 通知 outbox → 文本及
K 线图渲染 → Mock 投递 → 日结 → 只读投影”的完整链路。

该结论不等于 `MINUTE_DATA_ONLY_ACCEPTED`、`TRANSACTIONAL_PAPER_SHADOW_ACCEPTED` 或
`ACCEPT_PAPER_EXECUTION`。真实分钟执行仍由自然时间门禁保护。

## 本轮七项工作与逐项自测

1. 版本一致只读投影：实现账户、余额、持仓、订单、成交、风险、应收、日结和事件投影；
   6 项定向测试通过。
2. D0/M1/M2 固定输入回放：实现确定性配对结果、状态与哈希；18 项定向测试通过。
3. 一键 Mock 场景：实现可重复的单账户交易日 CLI；13 项测试及一次 CLI 实跑通过。
4. 通知状态机：实现文字/图像分片、审计、UNKNOWN 恢复、退避及人工处置；27 项测试通过。
5. Mock 全链路：同一场景实际生成 Markdown、PNG 并由无网络 transport 投递；4 项测试通过。
6. 企业微信桥接：实现群机器人 Webhook transport、密钥隔离与错误分类；12 项测试通过，
   未向真实群发送验收消息。
7. 安全激活和项目内调度：实现不可篡改激活证书、运行时准入校验、真实分钟 snapshot 消费
   作业，以及分钟执行和通知的显式调度开关；26 项组合测试通过。

各组测试存在重叠，数字用于记录每一步完成时执行的测试命令，不应相加理解为唯一用例数。

## 默认安全性

- `configs/service/service_v1.json` 保持 `account_writes_enabled=false` 和
  `minute_execution_admission=ACCEPT_DATA_ONLY`。
- 新增的 `minute.execute_paper_shadow` 与 `notification.deliver` 虽存在于作业目录，但没有显式
  handler 时处于 deferred 状态。
- 开启分钟账户写入必须同时满足：M5 准入结果通过、激活证书校验通过、隔离 shadow 配置、
  显式 `--enable-paper-shadow`、稳定账户 ID 和 M1/M2 政策。
- 开启企业微信必须显式 `--enable-notifications`，Webhook 只从环境或本地 env 文件读取。
- 企业微信接口没有端到端幂等保证，因此系统承诺持久化重试与重复可观测，不承诺绝不重复。

## 可复现命令

Mock 全链路：

```bash
.venv/bin/python scripts/run_mock_paper_scenario.py --output /tmp/abu-mock-paper
```

只做安全配置和数据库检查：

```bash
.venv/bin/python scripts/run_abu_service.py check
```

真实分钟 shadow 和通知只能在准入证书生成后使用示例配置显式启动：

```bash
.venv/bin/python scripts/run_abu_service.py run \
  --config configs/service/service_paper_shadow_v1.example.json \
  --enable-minute-shadow --enable-paper-shadow \
  --paper-account-id ACCOUNT_ID --execution-policy-id M1 \
  --enable-notifications
```

## 尚未通过的门禁

1. 从首个成功归档交易日起至少 10 个有效交易日采集和校准；
2. 冻结分钟覆盖率、延迟、revision 和质量阈值；
3. 冻结后重新连续观察至少 20 个有效交易日；
4. 用真实归档 snapshot 完成 D0/M1/M2 正式配对与独立组合回测；
5. M8 运维、恢复与可视化验收；
6. M9 单写者切换和正式模拟账户连续运行验收。

在这些门禁完成前，允许继续采集实时数据和运行无账户写入的 shadow，不能将默认服务配置
改为账户写入，也不能接管现有正式模拟账户。
