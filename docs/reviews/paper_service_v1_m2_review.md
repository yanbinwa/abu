# 自闭环多策略模拟交易系统 v1 M2 Review

| 字段 | 结果 |
| --- | --- |
| 日期 | 2026-10-05 |
| 基线提交 | `3030291a5057b36d7b9673e7ddd339df7437e572` |
| Review 结论 | `EVENT_AND_SNAPSHOT_CORE_ACCEPTED` |
| 账户写入 | 关闭 |
| 行情采集 | 未接入 |
| 自动任务变更 | 无 |

## 1. 完成内容

- `ContentAddressedStore`：规范 JSON、SHA-256 内容寻址、同文件系统临时文件、原子
  rename、文件和父目录 fsync；
- `DomainEventStore`：确定性事件 ID、payload 哈希、严格 stream sequence、前驱校验、
  重复写入幂等和碰撞失败关闭；
- 消费者登记、生效序号、必需消费者、退休边界和人工归档资格审查；
- `EventDispatcher`：持久化水位线、缺口等待/阻断、业务写入与消费确认同事务、启动恢复；
- 迟到事件分类：已形成交易事实后只允许追加审计更正，不改写历史交易事实；
- `SnapshotCatalog`：JSON Schema 校验、manifest 文件发布、`COMMITTED` 快照与领域事件
  同事务提交、只读查询、孤儿和损坏文件审计；
- `requirements.txt` 显式冻结 `jsonschema==4.26.0`。

M2 没有接入策略、账户、真实行情和通知，也没有启动常驻服务。

## 2. 一致性结论

权威提交顺序为：

```text
校验 manifest
-> 同文件系统写临时文件并 fsync
-> 原子 rename 并 fsync 父目录
-> SQLite 单事务写 COMMITTED snapshot + domain event
-> 消费者在单事务内写业务结果 + consumption + watermark
```

文件写入后、数据库事务前失败只会留下可审计孤儿；数据库事务提交后，即使首次分发前退出，
就绪事件仍可由启动恢复扫描发现。消费者 handler 只允许通过传入的 SQLite connection 写入
权威业务状态，防止业务生效但确认丢失。

## 3. 自测与故障注入

### 3.1 M2 目标测试

```text
.venv/bin/python -m unittest \
  tests.test_domain_event_store \
  tests.test_event_dispatcher \
  tests.test_market_snapshot_catalog -v
Ran 13 tests
OK
```

覆盖：

- 事件 ID、payload 哈希、sequence 和 previous event；
- 同 ID 同内容幂等、同 ID 不同内容阻断 stream 并生成 CRITICAL finding；
- 消费者生效序号、退休边界、必需消费者和禁止自动删除；
- handler 业务写入后异常时，业务、确认和水位线整体回滚；
- 已消费事件重复投递时不再次调用 handler；
- 缺序号进入 `WAITING_FOR_GAP`，超时后 `BLOCKED`，水位线不前进；
- 重启恢复消费持久化事件；
- 迟到分钟修订只追加审计更正；
- manifest 已落盘、事务前失败时只留下孤儿；
- snapshot 已插入、event 写入失败时 SQLite 整体回滚；
- manifest schema、前驱、文件哈希和损坏转 `CORRUPT`。

### 3.2 M0—M2 累计目标测试

```text
Ran 34 tests
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
Ran 426 tests
OK
```

### 3.5 静态检查

- `compileall abupy/ServiceBu`：通过；
- `git diff --check`：通过；
- 运行环境 `jsonschema`：4.26.0。

## 4. M3 约束

M3 只把现有日频数据能力适配为版本化组件和统一快照，先写新目录并与旧输出逐字段对账。
不得临时关闭缺失因子，不写正式账户，不停止旧数据任务，不改变现有策略规则。
