# Alpha158 ML M4 LambdaRank 插件验证

- 状态：`PASS`
- 依赖：LightGBM 4.6.0；wheel SHA256 `2dafd98d4e02b844ceb0b61450a660681076b1ea6c7adb8c566dfd66832aafad`；libomp 23.1.3。
- 本机运行前置：`DYLD_LIBRARY_PATH=$(brew --prefix libomp)/lib`。缺少该条件时明确失败，不安装或切换替代模型。
- 范围：完整等级映射、小 query/并列处理、训练 query 等权、验证日期等权、手工 NDCG 对账、只按 NDCG@10 早停、单线程确定性。
- 自测：M4 相关累计 13 项通过；全 M0--M5 聚焦套件在 M5 完成后共 36 项通过。
- 限制：插件与指标实现通过不等于历史筛选通过。

