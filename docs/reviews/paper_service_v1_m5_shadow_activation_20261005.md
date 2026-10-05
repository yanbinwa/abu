# M5 共享分钟行情 Data-only 激活记录

| 字段 | 结果 |
| --- | --- |
| 激活日期 | 2026-10-05 |
| 最终源提交 | `d12f7d4462ad8bc2f9659dc779fcfcd9de672a87` |
| 模式 | minute data-only shadow |
| 长期服务 | `com.abupy.paper-service` |
| 首个合格交易日 | 2026-10-09 |
| 账户写入 | false |
| 券商连接 | false |
| 分钟执行准入 | `ACCEPT_DATA_ONLY` |

## 1. 冻结运行环境

LaunchAgent 运行不可变发布目录：

```text
/Users/wjy/abu/service/paper_v1/runtime/releases/
d12f7d4462ad8bc2f9659dc779fcfcd9de672a87
```

`runtime_manifest.json` SHA-256：

```text
84cbdacf0fd68d6487210c8446c31a0cef2f84f11551fddaddcc9109e6eb9ce6
```

LaunchAgent plist SHA-256：

```text
edef5feaa85fb6cc5a7406664f0b1672c883abc2428a341cf2d2a14856de695d
```

专用 Python 3.11 环境的直接冻结依赖：

- numpy 1.23.5；
- pandas 1.5.3；
- APScheduler 3.11.3；
- jsonschema 4.26.0；
- AKShare 1.18.21。

服务实例已经从冻结 manifest 正确记录源提交，不再将冻结目录错误标记为 `UNKNOWN`。

## 2. 激活后的调度边界

已注册：

```text
minute.collect
daily.snapshot_shadow
```

仍为 deferred：

```text
calendar.preflight
preopen.snapshot
daily.collect
daily.strategy_shadow
daily.reconcile
```

分钟任务在 2026-10-09 之前、非交易日和盘外窗口均成功结束但不发布 snapshot。配置固定为
`DATA_ONLY`；它只采集共享行情、写不可变 Bar、发布市场 snapshot 和审计记录，不读取分钟
结果推进账户。

## 3. 首次真实调度发现与修复

第一次从提交 `9c0c362b5ecf7540091291d238b6e56ee3700b03` 启动时，APScheduler 工作
线程触发了 SQLite 默认的同线程限制。失败发生在创建 `job_run` 之前，没有发布分钟快照，
也没有产生账户事件。

修复措施：

- SQLite 连接明确启用跨线程访问；
- 所有事务用进程内 `RLock` 串行化；
- integrity check、backup 和 close 使用同一把锁；
- 新增四个工作线程共享同一 store 的串行事务测试；
- 全量回归增加到 471 项并全部通过。

修复后的真实 `minute.collect` 在 11:50 和最终版本 11:52 均为 `SUCCEEDED`。因日期早于
首个合格交易日，两次 `output_snapshot_ids_json=[]`，符合失败关闭设计。

## 4. 激活后核验

- LaunchAgent 状态：`running`；
- `recovered_instances=0`、`recovered_attempts=0`；
- SQLite `integrity_check=ok`、`foreign_key_errors=0`；
- `running_attempts=0`；
- `MINUTE` snapshot 数量为 0（首个合格日期尚未到达）；
- `account_events=0`；
- 心跳持续显示 `account_writes_enabled=false`、`broker_connected=false`。

## 5. 观察窗口

本记录证明 M5 data-only 采集服务已安全激活，不代表分钟数据已通过准入。自然观察从
2026-10-09 或之后首个成功归档交易日开始，需满足：

- 至少 10 个有效交易日；
- 每日不少于 180 个 committed 分钟 snapshot；
- 平均证券覆盖率不低于 95%；
- p99 延迟不高于 15 秒；
- 每日 gap 为 0；
- 无未解决 `ERROR/CRITICAL` 审计项。

门禁通过前，状态保持 `ACCEPT_DATA_ONLY`，M6 不得用分钟结果改变任何账户。
