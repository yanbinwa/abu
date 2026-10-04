# 日线选股与分钟执行系统 v1 — MS6 Review

## 结果

VCP 回测入口新增可选 `execution_policy_id`，默认 D0。M1/M2 从分钟存储或调用方注入的 loader 获取事件，并在独立的 `PortfolioExecutor` 中推进账户、持仓、预留和退出状态。

## 路径隔离

- 每次 D0/M1/M2 调用都创建独立执行器。
- 分钟无数据时失败关闭，不回退到 D0，也不自动替换其他股票。
- 卖出路径仍使用原日线开盘政策。
- 审计输出包含完整 intraday order events。
- 共享执行器的完整回归继续覆盖 VCP 与 Alpha158。
