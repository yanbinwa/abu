# 自闭环多策略模拟交易系统 v1 M3 Review

| 字段 | 结果 |
| --- | --- |
| 日期 | 2026-10-05 |
| 软件实现 | 完成 |
| 阶段准入 | 未通过；等待至少 5 个有效交易日 shadow 观察 |
| 当前结论 | `M3_SOFTWARE_READY_SHADOW_PENDING` |
| 账户写入 | 关闭 |
| 旧任务变更 | 无 |

## 1. 完成内容

- `DailyRawArchive`：原始响应和采集元数据内容寻址归档，敏感参数自动脱敏；
- 交易日增量计划和可注入的供应商请求速率限制；
- 原始成交价与复权信号价的显式 `price_space` 规范化和 OHLC 质量门禁；
- 基于 `available_at`、交易日和 revision 的 PIT 记录选择；
- 现有 `SelectionPanelV2` 数据目录的只读组件适配和稳定版本哈希；
- `DailySnapshotBuilder`：必需组件、可得时间、PIT 覆盖、组件血缘和快照提交；
- `FactorSnapshotBuilder`：冻结因子集合、源日频快照和不可变因子 payload；
- required / optional / display-only 字段依赖和版本化缺失政策；
- VCP、Alpha158、价值质量研究三套独立依赖门禁；
- `SelectionPanelV2` 逐字段精确对账函数和覆盖报告；
- `build_daily_service_snapshot.py` 只读审计/显式发布入口。
- 18:45/18:50/18:55 的数据-only shadow 快照 handler；每个交易日共享一个业务
  `job_run`，重试保留独立 attempt，成功后不重复执行。

## 2. 当前真实数据只读审计

对 `/Users/wjy/abu/data` 的现有数据只做读取和 SHA-256 计算，没有发布新快照：

| 组件 | 状态 | 文件数 |
| --- | --- | ---: |
| universe | AVAILABLE | 1 |
| calendar | AVAILABLE | 1 |
| security_master | AVAILABLE | 1 |
| raw_price | AVAILABLE | 5,448 |
| adjusted_price（含指数与 extra） | AVAILABLE | 5,452 |
| industry | AVAILABLE | 1 |
| corporate_action | AVAILABLE | 1 |
| limit_reference 血缘 | AVAILABLE | 5,449 |
| valuation | MISSING | 0 |
| fundamental_pit | MISSING | 0 |

因此：

- 冻结 VCP 依赖可解析；
- 冻结 Alpha158 所需行业组件可解析；
- 价值/质量研究因缺少规范化估值和基本面 PIT 组件而失败关闭；
- 缺 PE 不会被填零，也不会在运行时临时删除估值因子。

本次盘点只证明现有文件可被版本化，不证明 2026-09-30 当日收盘时已经可得。导入适配器要求
单独传入可审计的 `source_available_at`，禁止把后采集文件冒充历史 PIT 输入。

## 3. 自测

### 3.1 M3 目标测试

```text
.venv/bin/python -m unittest \
  tests.test_daily_data_center \
  tests.test_daily_shadow_job -v
Ran 12 tests
OK
```

覆盖：

- 同内容原始采集和日频快照幂等；
- 修改必需组件后生成新 revision；
- 必需组件缺失、晚于 cutoff 和 PIT 覆盖不足时失败关闭；
- 缺估值时价值/质量策略被阻断，VCP 不受未声明因子影响；
- 原始成交价和复权信号价不允许混合价格空间；
- 未来可得的估值 revision 不进入历史决策；
- 同一源快照与因子 payload 重建结果稳定；
- 组件文件版本、覆盖报告和 `SelectionPanelV2` 字段对账。

### 3.2 M0—M3 累计目标测试

```text
Ran 52 tests
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
Ran 444 tests
OK
```

### 3.5 静态和入口检查

- `compileall`：通过；
- `build_daily_service_snapshot.py --help`：通过；
- `run_abu_service.py check --enable-daily-shadow`：通过，数据库完整性为 `ok`；
- launchd 示例 `plutil -lint`：通过；
- `git diff --check`：通过。

## 4. 尚未满足的 M3 退出条件

截至 2026-10-05，尚未发生规格要求的 5 个有效交易日 shadow 运行，不能将软件单测或历史
重放冒充自然时间验证。因此当前不能给出 `DAILY_DATA_SHADOW_ACCEPTED`，也不应开始依赖 M3
正式准入的 M4 账户适配。

后续观察必须记录：

1. 每个有效交易日的 committed snapshot 和 domain event；
2. 必需作业是否在 19:00 前完成；
3. 与旧入口逐字段差异及原因码；
4. 服务重启后的恢复结果；
5. 连续 5 日成功率；任何遗漏或超时都会重置窗口。

2026-10-05 已从提交 `fcb424b9a4be0ac1f10070129dc2619893e16318` 冻结最小运行包，
并启动 data-only LaunchAgent。运行心跳确认只注册 `daily.snapshot_shadow`；账户、分钟、
通知和策略作业均为 deferred。旧 Codex 自动任务仍是现有数据和账户的唯一写入者。部署
证据见 `paper_service_v1_m3_shadow_activation_20261005.md`。
