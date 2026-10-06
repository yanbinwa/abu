# Alpha158 正交信号前瞻准备

已建立三个信号族的冻结处理方式，没有使用收益或 IC 选择公式，没有生成综合分数，也没有启用交易过滤。

后续增益评价以股票市场收益为唯一收益来源，不计入现金管理收益。

| 信号族 | 当前状态 | 说明 |
|---|---|---|
| 行业环境 | 前瞻 shadow | 保存行业超额强度、宽度和量能；历史年度稳定性未通过 |
| 财务质量与增长 | 前瞻 shadow | 保存 ROE、利润率、现金转换、负债率、收入与利润同比、资本开支强度 |
| 公告意外 | 阻塞 | 没有一致预期基准，且历史修订链不完整 |

财务快照覆盖 5,448 只证券，七个预登记特征同时可用的证券为 5,345 只，联合覆盖 98.109%。其最早可用时间固定为实际采集完成时间，禁止回填历史。特征保留原始值，尚未建立截尾、标准化或权重；当前观察到少数极端财务比率，后续只能按事先固定的经济有效性规则处理，不能根据收益挑选截尾点。

[完整前瞻注册报告](/Users/wjy/abu/shadow/alpha158_orthogonal_signals_v1/REPORT.md)

[就绪状态](/Users/wjy/abu/shadow/alpha158_orthogonal_signals_v1/readiness_report.json)

[财务特征快照](/Users/wjy/abu/shadow/alpha158_orthogonal_signals_v1/financial_quality_growth_snapshot.csv.gz)
