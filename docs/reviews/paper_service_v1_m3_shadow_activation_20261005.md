# M3 日频数据 Shadow 激活记录

| 字段 | 结果 |
| --- | --- |
| 激活日期 | 2026-10-05 |
| 源提交 | `fcb424b9a4be0ac1f10070129dc2619893e16318` |
| 模式 | data-only shadow |
| 长期服务 | `com.abupy.paper-service` |
| 账户写入 | false |
| 券商连接 | false |
| 分钟执行准入 | `ACCEPT_DATA_ONLY` |

## 1. 冻结运行环境

LaunchAgent 不直接加载工作区，而是运行不可变发布目录：

```text
/Users/wjy/abu/service/paper_v1/runtime/releases/
fcb424b9a4be0ac1f10070129dc2619893e16318
```

运行 manifest SHA-256：

```text
60f5ac704c5274b71d0ff7e6d889e023173183699ece58448e7f7ef5719be476
```

专用 Python 3.11 环境仅安装：

- numpy 1.23.5；
- pandas 1.5.3；
- APScheduler 3.11.3；
- jsonschema 4.26.0；
- 上述包的传递依赖。

LaunchAgent plist SHA-256：

```text
b482095d92037575b4fb88ffa669726ddb2423dd0de6a059917dd7c68e6b31a2
```

## 2. 激活后的调度边界

心跳中的注册作业：

```text
daily.snapshot_shadow
```

显式 deferred：

```text
calendar.preflight
preopen.snapshot
minute.collect
daily.collect
daily.strategy_shadow
daily.reconcile
```

因此当前服务只在 18:45、18:50、18:55 检查旧数据入口是否已经形成同日完整数据，并将
其发布为新目录下的版本化 shadow 快照。它不运行策略、不写账户、不发通知、不采集分钟
行情，也不停止任何旧任务。

## 3. 恢复验证

- 第一次热切换发现历史实例缺少停止记录；新实例启动时将 2 条陈旧实例标记为
  `SERVICE_RESTART_DETECTED`；
- 增加 SIGTERM 处理后再次执行 `launchctl bootout`，实例以 `signal:15` 正常停止；
- 随后重启的 `recovered_instances=0`、`recovered_attempts=0`；
- 当前 LaunchAgent 状态为 `running`，心跳状态为 `RUNNING`；
- SQLite `integrity_check=ok`、`foreign_key_errors=0`、`running_attempts=0`；
- 服务锁保证同一时刻只有一个运行实例。

## 4. 观察窗口

本记录只代表 shadow 服务成功激活，不代表 M3 已准入。连续 5 个有效交易日窗口应从首个
成功提交同日快照的交易日开始计算。每个有效日必须在 19:00 前完成，且新旧数据差异均有
明确原因码；任何遗漏或超时都会重置窗口。
