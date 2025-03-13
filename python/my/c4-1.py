# 基础库导入，注意第一次运行时会比较慢，要做数据的解压等处理操作
from __future__ import print_function
from __future__ import division

import warnings

warnings.filterwarnings('ignore')
warnings.simplefilter('ignore')

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os
import sys

# 使用insert 0即只使用github，避免交叉使用了pip安装的abupy，导致的版本不一致问题
sys.path.insert(0, os.path.abspath('../../'))
import abupy

from abupy import AbuFactorBuyXD, BuyCallMixin

class AbuFactorBuyBreak(AbuFactorBuyXD, BuyCallMixin):
    """示例继承AbuFactorBuyXD完成正向突破买入择时类, 混入BuyCallMixin，即向上突破触发买入event"""
    def fit_day(self, today):
        """
        针对每一个交易日拟合买入交易策略，寻找向上突破买入机会
        :param today: 当前驱动的交易日金融时间序列数据
        :return:
        """
        # 今天的收盘价格达到xd天内最高价格则符合买入条件
        if today.close == self.xd_kl.close.max():
            # 生成买入订单, 由于使用了今天的收盘价格做为策略信号判断，所以信号发出后，只能明天买
            return self.buy_tomorrow()
        return None

abupy.env.enable_example_env_ipython()


from abupy import AbuFactorBuyBreak, AbuFactorSellBreak
from abupy import AbuFactorAtrNStop, AbuFactorPreAtrNStop, AbuFactorCloseAtrNStop
from abupy import ABuPickTimeExecute, AbuBenchmark, AbuCapital

# buy_factors 60日向上突破，42日向上突破两个因子
buy_factors = [{'xd': 60, 'class': AbuFactorBuyBreak},
               {'xd': 42, 'class': AbuFactorBuyBreak}]
# 四个卖出因子同时并行生效
sell_factors = [
    {
        'xd': 120,
        'class': AbuFactorSellBreak
    },
    {
        'stop_loss_n': 0.5,
        'stop_win_n': 3.0,
        'class': AbuFactorAtrNStop
    },
    {
        'class': AbuFactorPreAtrNStop,
        'pre_atr_n': 1.0
    },
    {
        'class': AbuFactorCloseAtrNStop,
        'close_atr_n': 1.5
    }]


benchmark = AbuBenchmark()
capital = AbuCapital(1000000, benchmark)

# 我们假定choice_symbols是我们选股模块的结果，
choice_symbols = ['usTSLA', 'usNOAH', 'usSFUN', 'usBIDU', 'usAAPL',
                  'usGOOG', 'usWUBA', 'usVIPS']

capital = AbuCapital(1000000, benchmark)
orders_pd, action_pd, all_fit_symbols_cnt = ABuPickTimeExecute.do_symbols_with_same_factors(choice_symbols,
                                                                                            benchmark,
                                                                                            buy_factors,
                                                                                            sell_factors,
                                                                                            capital,
                                                                                            show=False)

orders_pd
action_pd

from abupy import AbuMetricsBase
metrics = AbuMetricsBase(orders_pd, action_pd, capital, benchmark)
metrics.fit_metrics()
metrics.plot_returns_cmp(only_show_returns=True)