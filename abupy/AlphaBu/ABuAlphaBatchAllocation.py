"""Research-only, close-time batch risk allocation with unchanged hard limits."""
from collections import Counter
import copy
import hashlib
import numpy as np

from .ABuPortfolioRisk import PortfolioRiskEngine, _lots


def clone_account(executor):
    """Isolate approval mutations without copying the immutable price panel."""
    other = copy.copy(executor)
    for key, value in vars(executor).items():
        if key not in ('panel', 'config'):
            setattr(other, key, copy.deepcopy(value))
    return other


class BatchRiskAllocator:
    """Equal initial risk within a fixed top-ranked, individually feasible pool.

    No redistribution of lot-rounding leftovers and no same-review refill after
    plan/fill rejection. Pending sells do not release risk early. Candidate pool
    formation is rank-based; submission order within that pool is irrelevant to
    the equal-risk plan. Existing execution/risk checks remain authoritative.
    """

    def __init__(self, mode='equal_risk', submission_order='rank', diagnose=False):
        if mode not in ('equal_risk', 'sequential_pool'):
            raise ValueError('unknown allocation mode')
        if submission_order not in ('rank', 'reverse'):
            raise ValueError('unknown submission order')
        self.mode, self.submission_order, self.diagnose = mode, submission_order, diagnose
        self.plans, self.order_diagnostics = [], []

    def prepare(self, entries, slots, features, risk, executor, day, entry_day,
                held_or_ordered, score_column):
        if slots <= 0:
            return []
        probe = PortfolioRiskEngine(risk.panel, risk.config)
        selected = []
        seen = set(held_or_ordered)
        for row in sorted(entries, key=lambda r:(int(r['daily_rank']), str(r['symbol']))):
            symbol = str(row['symbol'])
            if symbol in seen:
                continue
            seen.add(symbol)
            intent = features.make_intent(day, int(row['column']), float(row[score_column]))
            if intent is None:
                continue
            if intent.side != 'buy' or (intent.position_effect or 'OPEN') != 'OPEN':
                raise ValueError('batch allocator supports fresh long entries only')
            decision = probe.evaluate(executor, intent, day, entry_day)
            if decision.final_quantity < 100:
                continue
            selected.append((row, intent, decision))
            if len(selected) == slots:
                break
        if not selected:
            return []
        quantities = self.equal_quantities(selected, risk, executor, day)
        if self.diagnose:
            self.diagnose_order(selected, quantities, risk, executor, day, entry_day)
        result = []
        for row,intent,decision in selected:
            quantity = quantities[intent.symbol]
            self.plans.append(dict(signal_asof=int(risk.panel.dates[day]),symbol=intent.symbol,
                daily_rank=int(row['daily_rank']),pool_size=len(selected),slots=slots,
                individual_cap_quantity=decision.final_quantity,planned_quantity=quantity,
                planned_risk_cash=quantity*decision.planned_risk_per_share,
                allocation_mode=self.mode,submission_order=self.submission_order))
            result.append((row,intent,quantity if self.mode=='equal_risk' else None))
        return result[::-1] if self.submission_order=='reverse' else result

    def equal_quantities(self, selected, risk, executor, day):
        equity,gross,open_risk,industries,same_day = risk._state(executor,day)
        cfg = risk.config
        budget = max(0., min(equity*cfg.portfolio_open_risk_fraction-open_risk,
                            equity*cfg.same_day_new_risk_fraction-same_day))
        groups = {intent.symbol:risk._industry(day,risk.panel.symbol_index[intent.symbol])
                  for _,intent,_ in selected}
        counts = Counter(groups.values())
        caps, prices, candidate_rows = {}, {}, {}
        for _,intent,decision in selected:
            symbol = intent.symbol
            column = risk.panel.symbol_index[symbol]
            industry = groups[symbol]
            per_trade = min(equity*cfg.single_trade_risk_fraction,budget/len(selected),
                max(0.,equity*cfg.industry_open_risk_fraction-industries.get(industry,0.))/counts[industry])
            caps[symbol] = min(decision.final_quantity,_lots(per_trade/decision.planned_risk_per_share))
            raw = float(intent.signal_price_raw or risk.panel.exec_close[day,column])
            price = float(intent.metadata.get('max_buy_price_raw',raw*(1+float(intent.metadata.get('max_gap_fraction',.03)))))
            prices[symbol] = price
            candidate_rows[symbol] = dict(industry=industry,beta=risk.beta_for(day,column),
                has_stop=True,limit_fraction=risk._limit_fraction(day,column),pending=True)
        existing = risk._portfolio(executor,day)
        symbols = sorted(caps)
        def scaled(scale):
            return {symbol:_lots(caps[symbol]*scale) for symbol in symbols}
        def feasible(quantities):
            rows = list(existing)
            cost = 0.
            for _,intent,decision in selected:
                symbol = intent.symbol
                quantity = quantities[symbol]
                if not quantity:
                    continue
                cost += quantity*prices[symbol]+sum(executor._fees(quantity,prices[symbol],'buy'))
                rows.append(dict(candidate_rows[symbol],symbol=symbol,quantity=quantity,
                    market_value=quantity*prices[symbol],open_risk=quantity*decision.planned_risk_per_share,
                    initial_r_cash=quantity*decision.planned_risk_per_share))
            if cost>executor.available_cash+1e-9:
                return False
            if sum(r['market_value'] for r in rows)>equity*cfg.max_gross_exposure+1e-9:
                return False
            losses=risk._stress_from_rows(rows)
            return max(losses[k] for k in ('MARKET_7','INDUSTRY_10','MARKET_5_PLUS_INDUSTRY_5','GAP_2R'))<=equity*cfg.max_stress_loss_fraction+1e-9
        if feasible(caps):
            return caps
        if not feasible(scaled(0)):
            return scaled(0)
        lo,hi = 0.,1.
        for _ in range(48):
            middle=(lo+hi)/2
            if feasible(scaled(middle)):
                lo=middle
            else:
                hi=middle
        result=scaled(lo)
        assert feasible(result)
        return result

    def diagnose_order(self, selected, quantities, risk, executor, day, entry_day):
        orders = dict(rank=selected,reverse=selected[::-1],
            fixed_hash=sorted(selected,key=lambda x:hashlib.sha256((str(int(risk.panel.dates[day]))+':'+x[1].symbol).encode()).hexdigest()))
        paths = {}
        for mode in ('sequential','equal_risk'):
            for order_name, sequence in orders.items():
                account = clone_account(executor)
                engine = PortfolioRiskEngine(risk.panel,risk.config)
                result = {}
                for _,intent,_ in sequence:
                    requested=quantities[intent.symbol] if mode=='equal_risk' else None
                    order,_,decision=engine.approve(account,intent,day,entry_day,requested_quantity=requested)
                    result[intent.symbol]=order.quantity if order is not None else 0
                    if mode=='equal_risk' and result[intent.symbol]!=requested:
                        raise AssertionError('batch plan failed unchanged risk/execution approval')
                paths[(mode,order_name)]=result
        for _,intent,decision in selected:
            self.order_diagnostics.append(dict(signal_asof=int(risk.panel.dates[day]),symbol=intent.symbol,
                planned_risk_per_share=decision.planned_risk_per_share,
                **{mode+'_'+order:paths[(mode,order)][intent.symbol]
                   for mode in ('sequential','equal_risk') for order in orders}))
