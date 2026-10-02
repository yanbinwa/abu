# 沪深 A 股波动仓位混合策略

`volatility_blend` 将已有的按周、按月买入因子合在一个账户中，并以信号日的 ATR21 调整每笔仓位。它是只能做多的现货策略：目标是利用股价波动中的趋势机会，同时在振幅扩大时降低单笔暴露；它并不能像期权多头那样仅凭波动扩大获利。

## 固定规则

- 月度信号：基准指数日历的月末交易日收盘后发出，下一市场交易日买入；单笔名义金额上限为 ABU 当时记录可用现金的 8%。
- 周度信号：基准指数日历的周五收盘后发出，下一市场交易日买入；单笔名义金额上限为 ABU 当时记录可用现金的 4%。
- 如果股票下一根行情不是下一市场交易日，取消该次信号，避免停牌前信号跨停牌成交。
- 波动仓位：`仓位比例 = min(上限, 风险系数 / (2 × 信号日 ATR21 / 信号日收盘价))`。月度风险系数为 0.006，周度为 0.003。只用信号日及之前的数据；买入价由下一交易日 ABU 模拟成交价确定。
- 退出沿用 `AbuFactorSellNDay(sell_n=20)`。资金不足的买单被拒，其配对卖单也不能执行。
- 周/月信号叠加会使账户接近满仓。组合执行时建议使用 `--max-gross-exposure 0.8 --max-symbol-weight 0.12`，在买入前按上一交易日收盘估值检查持仓上限；上限也需要接受含滑点的回测检验。
- `volatility_blend_fixed` 使用相同信号和上限，但固定使用上限仓位，用于衡量波动调仓本身的影响。

本策略没有针对五年数据优化这些数值。原生 ABU 的资金账户会误处理部分失败买单的卖单，组合收益须用 `backtest_akshare_cn_strategies.py` 的逐订单资金账读取；但订单股数在生成时仍由原生账户可用现金决定，因此重算不能完全消除该问题对仓位的影响。

[五年验证报告](/Users/wjy/abu/backtests/akshare_cn_2021_2026/HYBRID_REPORT.md)显示：三组样本的 ATR 仓位版最大回撤较固定仓位版降低约 5–8 个百分点；加入总仓位上限、一字板拒单及双边各 25bp 滑点后，一组样本转为亏损。目前只适合作为继续验证的研究策略。

## 运行

在仓库根目录执行，例如：

```bash
.venv/bin/python scripts/backtest_akshare_cn_strategies.py \
  --start 2021-10-02 --end 2026-10-02 \
  --sample-size 400 --seed 20261002 \
  --snapshot-dir /Users/wjy/abu/backtests/akshare_cn_2021_2026/data_snapshot \
  --output-dir /Users/wjy/abu/backtests/akshare_cn_2021_2026/blend_capped_seed_20261002 \
  --max-gross-exposure 0.8 --max-symbol-weight 0.12 \
  --only-groups hybrid --only volatility_blend volatility_blend_fixed
```

实现位于 [买入因子](/Users/wjy/Documents/code/abu/abupy/FactorBuyBu/ABuFactorBuyVolatilityHybrid.py)、[波动仓位](/Users/wjy/Documents/code/abu/abupy/BetaBu/ABuVolatilityRiskPosition.py)。评估应同时查看三组随机股票样本、最大回撤、与固定仓位版的差异，以及取消一字板和增加滑点后的结果。

也可在 ABU 代码中调用 `from abupy.FactorBuyBu import build_volatility_blend`，然后用 `buy_factors, sell_factors = build_volatility_blend()` 获取完整因子配置。组合总持仓上限在回测脚本的订单资金账里执行；直接调用原生 `abu.run_loop_back` 不会自动应用该上限或修正原生资金账户的问题。
