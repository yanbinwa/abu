# 自闭环多策略模拟交易系统 v1 M4 软件 Review

## 1. 结论

| 项目 | 结果 |
| --- | --- |
| 评审日期 | 2026-10-05 |
| 覆盖范围 | M4-T01—T09 |
| 软件结论 | `M4_SOFTWARE_READY` |
| 自然时间账户结论 | `BLOCKED_BY_M3_DAILY_SHADOW_GATE` |
| M4 最终准入 | 未通过；M4-T10 尚未执行 |

本轮允许在 M3 的 5 个真实交易日观察期间并行完成不依赖自然时间的 M4 软件。所有新增
账户均只存在于临时测试数据库；没有修改正在运行的 LaunchAgent 发布目录，没有开启账户
写入，也没有接管现有模拟盘。

## 2. 已完成能力

### 2.1 稳定身份和 activation

- 新增 `StrategyAccountStore`，在现有 M0 schema 上管理策略实例、稳定账户和配置 activation；
- 配置采用规范化 JSON 哈希，键顺序不影响 `config_sha256`；
- `account_id` 与配置哈希解耦，同策略、同配置可创建不同实例和账户；
- activation 只允许在声明的交易日边界切换，旧 activation 自动封闭有效区间；
- 配置升级保留账户身份，旧持仓默认保留原管理 activation。

### 2.2 持仓管理归属

- 逻辑交易冻结开仓 activation、管理 activation 及入场、退出、风险政策版本；
- position lot 只能绑定同账户、同证券的开放逻辑交易；
- 接管必须逐笔执行，并在同一 SQLite 事务内写入
  `PositionManagementTakenOver` 领域事件、assignment 和交易管理归属更新；
- 不同账户之间的 activation 或 trade 绑定失败关闭。

### 2.3 交易 envelope 和策略插件

- `TradeIntent`、`ApprovedOrder`、`Fill`、`RiskDecision` 增加 `account_id`、
  `strategy_instance_id`、`actor_activation_id` 和 `source_snapshot_id`；
- 旧调用使用空默认值，因此历史策略接口和状态文件仍保持兼容；
- 新 M4 `PortfolioDomainCore` 要求四项来源身份齐全并与账户 binding 一致；
- VCP 与 Alpha158 适配器直接委托既有信号/退出实现，只增加账户命名空间；
- `AccountView` 使用 frozen dataclass 和只读 mapping，不向策略暴露写接口。

## 3. 自测结果

### 3.1 M4 专项测试

```text
.venv/bin/python -m unittest \
  tests.test_strategy_account_store \
  tests.test_strategy_plugins \
  tests.test_portfolio_risk.PortfolioRiskTest.test_m4_account_namespace_flows_into_risk_decision
Ran 12 tests
OK
```

覆盖：

- 同配置双实例账户隔离；
- 账户身份跨配置升级稳定；
- 旧持仓保留旧 activation；
- 逐笔 takeover 的原子事件和幂等恢复；
- 跨账户绑定失败关闭；
- VCP/Alpha158 适配前后意图逐字段一致，仅新增命名空间；
- 订单、成交和风险决策的账户身份传播；
- 领域核心与直接 `PortfolioExecutor` 调用结果一致。

### 3.2 冻结策略黄金回归

```text
.venv/bin/python -m unittest \
  tests.test_vcp_strategy tests.test_vcp_paper_pipeline \
  tests.test_alpha_forward_shadow tests.test_intraday_execution \
  tests.test_intraday_shadow tests.test_portfolio_executor
Ran 53 tests
OK
```

交易 envelope 的兼容扩展改变了 `ABuTradeIntent.py` 和 `ABuPortfolioExecutor.py` 文件哈希，
但未改变上述黄金结果。`golden_baselines_v1.json` 保存旧哈希、变更范围及新哈希，防止把这次
兼容性扩展误认为策略基线重写。

### 3.3 全量回归

```text
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 458 tests
OK
```

`python -m compileall -q abupy/ServiceBu abupy/AlphaBu` 通过，`git diff --check` 通过。

## 4. 尚未执行的工作

M4-T10 不在本轮执行。只有 M3 返回 `DAILY_DATA_SHADOW_ACCEPTED` 后，才可以：

1. 在服务数据库登记 `configs/service/accounts_v1.json` 中的两个独立 shadow 账户；
2. 让账户消费自然时间 committed daily snapshot；
3. 对账 VCP、Alpha158 的候选、意图、审批、日线退出和账户结果；
4. 在无未解释差异后给出 `MULTI_ACCOUNT_DAILY_SHADOW_ACCEPTED`。

因此本记录不是 M4 最终准入，也不授权 M6 或任何正式账户写入。
