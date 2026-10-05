# 自闭环多策略模拟交易系统 v1 M1 Review

| 字段 | 结果 |
| --- | --- |
| 日期 | 2026-10-05 |
| 基线提交 | `3030291a5057b36d7b9673e7ddd339df7437e572` |
| Review 结论 | `RUNTIME_KERNEL_ACCEPTED` |
| 账户写入 | 关闭 |
| 行情采集 | 未接入 |
| 自动任务变更 | 无 |

## 1. 完成内容

- `ServiceLock`：单机进程全生命周期 OS 排他文件锁；
- `OperationalStore`：SQLite WAL、外键、FULL synchronous、事务和完整性检查；
- `SchemaMigration`：v1 schema 哈希校验、原子创建、拒绝同版本内容漂移；
- `JobStore`：业务 run 与实际 attempt 分离、幂等运行和重启恢复；
- `ProjectScheduler`：APScheduler 3.11.3 薄适配，项目数据库仍是权威状态；
- `ServiceRuntime`：安全配置门禁、服务实例、心跳、作业登记和恢复；
- SQLite Online Backup、backup manifest 和空路径恢复；
- `check/run/audit/backup/restore` CLI；
- launchd 示例，仅负责启动和保活；
- `requirements.txt` 冻结 `APScheduler==3.11.3`。

## 2. 安全边界

默认配置继续强制：

```text
research_only = true
broker_connected = false
account_writes_enabled = false
minute_execution_admission = ACCEPT_DATA_ONLY
```

M1 没有启动默认常驻服务，没有创建或推进模拟账户，没有采集市场数据，没有发送通知，
也没有停止现有 Codex 自动任务。

## 3. 自测

### 3.1 M1 目标测试

```text
.venv/bin/python -m unittest \
  tests.test_service_lock \
  tests.test_operational_store \
  tests.test_job_store \
  tests.test_service_runtime
Ran 12 tests
OK
```

覆盖：

- 第二实例拒绝；
- 重复 acquire/release；
- schema 重复初始化和哈希一致；
- 事务整体回滚；
- WAL 在线备份、依赖哈希和空路径恢复；
- job run 幂等和多 attempt 历史；
- `RUNNING` attempt 在重启时转为可重试；
- 作业定义漂移拒绝；
- runtime 安全启动和停止；
- 账户写入配置拒绝；
- 只有显式 handler 才注册调度任务。

### 3.2 M0+M1 目标测试

```text
Ran 21 tests
OK
```

### 3.3 策略与执行相邻回归

```text
Ran 53 tests
OK
```

### 3.4 完整回归

```text
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 413 tests
OK
```

### 3.5 静态与部署入口检查

- `compileall`：通过；
- `plutil -lint deploy/launchd/com.abupy.paper-service.plist.example`：通过；
- `run_abu_service.py --help`：通过；
- backup/restore CLI 参数入口：通过。

首次 CLI smoke 检查发现脚本直接运行时缺少项目根目录导入路径，已修复四个新增 CLI，
随后重新运行全部检查和回归。

## 4. M2 约束

M2 只实现领域事件、消费者、水位线和快照目录，不接入策略和账户，不启动真实采集，
不改变现有自动任务。
