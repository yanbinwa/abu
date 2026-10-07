# Alpha158 ML M7 长历史七族 OOS 验证

- 实现状态：`PASS`
- 数据生产状态：`COMPLETE`
- 数据前置：`alpha158_history_2015_data_v1` 为 `COMPLETE`，3,054 只股票、3,825 个交易日、无缺失信号；面板抽样审计为 `PASS`。
- 七个因子族均已完成：每族 5,300,692 行、2,960 个信号日、47 个时间折。
- 七族共同键数：5,300,692；共同键 SHA256：`5c237544481d81c489394a50c0cd86a72ae62395dcd3cc4786a603f72cda98ae`。
- 来源合同审计：分片预测哈希、唯一键、预测日属于测试折、训练/验证标签成熟时间、模型可用时间和递归来源 manifest 全部通过。
- 审计产物：`/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007/ml_family_oos_audit.json`。
- 审计文件 SHA256：`f04360e6230cd98b94cffeb6ca356b6f1ac912e94911a7be0d636aa20a8b8382`。
- 自测：M7 审计与 OOS 仓库定向测试 9 项通过；正式审计命令重跑为 `PASS`。
- 门禁结论：允许 M8 读取七族共同 OOS 分数；不允许任一因子族缺失时缩短区间或静默降级。
