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

from abupy import AbuFactorBuyXD, BuyCallMixin, AbuPositionBase

abupy.env.enable_example_env_ipython()


from abupy import AbuFactorBuyBreak, AbuFactorSellBreak
from abupy import AbuFactorAtrNStop, AbuFactorPreAtrNStop, AbuFactorCloseAtrNStop
from abupy import ABuPickTimeExecute, AbuBenchmark, AbuCapital

class AbuKellyPosition(AbuPositionBase):
    """示例kelly仓位管理类"""

    def fit_position(self, factor_object):
        """
        fit_position计算的结果是买入多少个单位（股，手，顿，合约）
        需要factor_object策略因子对象通过历史回测统计胜率，期望收益，期望亏损，
        并设置构造当前factor_object对象，通过kelly公司计算仓位
        :param factor_object: ABuFactorBuyBases子类实例对象
        :return:买入多少个单位（股，手，顿，合约）
        """
        # 败率
        loss_rate = 1 - self.win_rate
        # kelly计算出仓位比例
        kelly_pos = self.win_rate - loss_rate / (self.gains_mean / self.losses_mean)
        # 最大仓位限制，依然受上层最大仓位控制限制，eg：如果kelly计算出全仓，依然会减少到75%，如修改需要修改最大仓位值
        kelly_pos = self.pos_max if kelly_pos > self.pos_max else kelly_pos
        # 结果是买入多少个单位（股，手，顿，合约）
        return self.read_cash * kelly_pos / self.bp * self.deposit_rate

    def _init_self(self, **kwargs):
        """kelly仓位控制管理类初始化设置"""

        # 默认kelly仓位胜率0.50
        self.win_rate = kwargs.pop('win_rate', 0.50)
        # 默认平均获利期望0.10
        self.gains_mean = kwargs.pop('gains_mean', 0.10)
        # 默认平均亏损期望0.05
        self.losses_mean = kwargs.pop('losses_mean', 0.05)

        """以默认的设置kelly根据计算0.5 - 0.5 / (0.10 / 0.05) 仓位将是0.25即25%"""


metrics = {
    'win_rate': 0.4179,
    'gains_mean': 0.12,
    'losses_mean': -0.49
}

# buy_factors 60日向上突破，42日向上突破两个因子
buy_factors = [{'xd': 60, 'class': AbuFactorBuyBreak},
               {'xd': 42, 'class': AbuFactorBuyBreak, 'position': {'class': AbuKellyPosition, 'win_rate': metrics.win_rate,
                                        'gains_mean': metrics.gains_mean, 'losses_mean': -metrics.losses_mean}}]
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

orders_pd[:10].filter(['symbol', 'buy_cnt', 'buy_factor', 'buy_pos'])
