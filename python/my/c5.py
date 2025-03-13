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

from abupy import AbuPickStockBase, ps, ABuRegUtil


class AbuPickRegressAngMinMax(AbuPickStockBase):
    """拟合角度选股因子示例类"""

    def _init_self(self, **kwargs):
        """通过kwargs设置拟合角度边际条件，配置因子参数"""

        # 暂时与base保持一致不使用kwargs.pop('a', default)方式
        # fit_pick中 ang > threshold_ang_min, 默认负无穷，即默认所有都符合
        self.threshold_ang_min = -np.inf
        if 'threshold_ang_min' in kwargs:
            # 设置最小角度阀值
            self.threshold_ang_min = kwargs['threshold_ang_min']

        # fit_pick中 ang < threshold_ang_max, 默认正无穷，即默认所有都符合
        self.threshold_ang_max = np.inf
        if 'threshold_ang_max' in kwargs:
            # 设置最大角度阀值
            self.threshold_ang_max = kwargs['threshold_ang_max']

    @ps.reversed_result
    def fit_pick(self, kl_pd, target_symbol):
        """开始根据自定义拟合角度边际参数进行选股"""
        # 计算走势角度
        ang = ABuRegUtil.calc_regress_deg(kl_pd.close, show=False)
        # 根据参数进行角度条件判断
        if self.threshold_ang_min < ang < self.threshold_ang_max:
            return True
        return False

    def fit_first_choice(self, pick_worker, choice_symbols, *args, **kwargs):
        raise NotImplementedError('AbuPickRegressAng fit_first_choice unsupported now!')


from abupy import AbuPickStockWorker
from abupy import AbuBenchmark, AbuCapital, AbuKLManager

# 选股条件threshold_ang_min=0.0, 即要求股票走势为向上上升趋势
stock_pickers = [{'class': AbuPickRegressAngMinMax,
                  'threshold_ang_min': 0.0, 'reversed': False}]

# 从这几个股票里进行选股，只是为了演示方便
# 一般的选股都会是数量比较多的情况比如全市场股票
choice_symbols = ['usNOAH', 'usSFUN', 'usBIDU', 'usAAPL', 'usGOOG',
                  'usTSLA', 'usWUBA', 'usVIPS']

benchmark = AbuBenchmark()
capital = AbuCapital(1000000, benchmark)
kl_pd_manger = AbuKLManager(benchmark, capital)
stock_pick = AbuPickStockWorker(capital, benchmark, kl_pd_manger,
                                choice_symbols=choice_symbols,
                                stock_pickers=stock_pickers)
stock_pick.fit()
# 打印最后的选股结果
print(stock_pick.choice_symbols)

kl_pd_noah = kl_pd_manger.get_pick_stock_kl_pd('usNOAH')
# 绘制并计算角度
deg = ABuRegUtil.calc_regress_deg(kl_pd_noah.close)
print('noah 选股周期内角度={}'.format(round(deg, 3)))
