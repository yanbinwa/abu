#!/usr/bin/env python3
"""Bounded Eastmoney statement client used by the full-market collector."""
from __future__ import annotations

import time
from collections import Counter
from datetime import date

import pandas as pd
import requests
from bs4 import BeautifulSoup


LISTED_ENDPOINTS = {
    "income": ("lrbDateAjaxNew", "lrbAjaxNew"),
    "balance": ("zcfzbDateAjaxNew", "zcfzbAjaxNew"),
    "cashflow": ("xjllbDateAjaxNew", "xjllbAjaxNew"),
}
DELISTED_ENDPOINTS = {
    "income": ("RPT_F10_FINANCE_GINCOME", "APP_F10_GINCOME"),
    "balance": ("RPT_F10_FINANCE_GBALANCE", "F10_FINANCE_GBALANCE"),
    "cashflow": ("RPT_F10_FINANCE_GCASHFLOW", "APP_F10_GCASHFLOW"),
}


class _StatementAdapter(object):
    def __init__(self, client, statement_type, is_delisted):
        self.client = client
        self.statement_type = statement_type
        self.is_delisted = is_delisted
        self.__name__ = "bounded_eastmoney_{}_{}".format(
            statement_type, "delisted" if is_delisted else "listed")

    def __call__(self, symbol):
        return self.client.statement_frame(
            symbol, self.statement_type, self.is_delisted)


class BoundedEastmoneyStatementClient(object):
    """Fetch only report periods required by the frozen research interval."""

    def __init__(self, start_date, end_date, timeout=30, max_attempts=3,
                 retry_backoffs=(2, 10), session=None, sleeper=time.sleep):
        self.start_date = date.fromisoformat(str(start_date)[:10])
        self.end_date = date.fromisoformat(str(end_date)[:10])
        if self.start_date > self.end_date:
            raise ValueError("report-period interval is inverted")
        self.timeout = float(timeout)
        self.max_attempts = int(max_attempts)
        if self.max_attempts <= 0:
            raise ValueError("request_max_attempts must be positive")
        self.retry_backoffs = tuple(float(value) for value in retry_backoffs)
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.company_types = {}
        self.delisted_dates = {}
        self.counters = Counter()

    def adapters(self):
        return {(statement, is_delisted): _StatementAdapter(
            self, statement, is_delisted)
                for statement in LISTED_ENDPOINTS
                for is_delisted in (False, True)}

    def metrics(self):
        return {
            **dict(sorted(self.counters.items())),
            "report_period_start_date": self.start_date.isoformat(),
            "report_period_end_date": self.end_date.isoformat(),
        }

    def _request(self, url, params, expect_json=True):
        last_error = None
        for attempt in range(self.max_attempts):
            self.counters["http_request_attempts"] += 1
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout,
                    headers={"User-Agent": "Mozilla/5.0 abu-research/1.0"})
                response.raise_for_status()
                return response.json() if expect_json else response.text
            except Exception as error:
                last_error = error
                if attempt + 1 >= self.max_attempts:
                    break
                self.counters["http_retries"] += 1
                delay = self.retry_backoffs[min(
                    attempt, len(self.retry_backoffs) - 1
                )] if self.retry_backoffs else 0
                if delay:
                    self.sleeper(delay)
        raise last_error

    def _company_type(self, symbol):
        if symbol not in self.company_types:
            body = self._request(
                "https://emweb.securities.eastmoney.com/PC_HSF10/"
                "NewFinanceAnalysis/Index",
                {"type": "web", "code": symbol.lower()},
                expect_json=False)
            node = BeautifulSoup(body, features="lxml").find(
                attrs={"id": "hidctype"})
            if node is None or not node.get("value"):
                raise ValueError("Eastmoney company type is unavailable")
            self.company_types[symbol] = node["value"]
        return self.company_types[symbol]

    def _bounded_dates(self, values):
        parsed = []
        for value in values:
            current = pd.to_datetime(value).date()
            if self.start_date <= current <= self.end_date:
                parsed.append(current.isoformat())
        return sorted(set(parsed), reverse=True)

    def _listed_frame(self, symbol, statement_type):
        company_type = self._company_type(symbol)
        catalog_name, detail_name = LISTED_ENDPOINTS[statement_type]
        base = ("https://emweb.securities.eastmoney.com/PC_HSF10/"
                "NewFinanceAnalysis/")
        common = {"companyType": company_type, "reportDateType": "0",
                  "code": symbol}
        catalog = self._request(base + catalog_name, common)
        catalog_rows = catalog.get("data") or []
        dates = self._bounded_dates(
            item.get("REPORT_DATE") for item in catalog_rows)
        self.counters["catalog_report_periods"] += len(catalog_rows)
        self.counters["selected_report_periods"] += len(dates)
        frames = []
        for offset in range(0, len(dates), 5):
            params = {**common, "reportType": "1",
                      "dates": ",".join(dates[offset:offset + 5])}
            body = self._request(base + detail_name, params)
            if "data" not in body:
                raise ValueError("Eastmoney listed detail response lacks data")
            frames.append(pd.DataFrame(body.get("data") or []))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _delisted_catalog(self, symbol):
        if symbol not in self.delisted_dates:
            url = "https://datacenter.eastmoney.com/securities/api/data/get"
            params = {
                "type": "RPT_F10_FINANCE_GINCOME",
                "sty": ("SECUCODE,SECURITY_CODE,REPORT_DATE,REPORT_TYPE,"
                        "REPORT_DATE_NAME"),
                "filter": '(SECUCODE="{}.{}")'.format(
                    symbol[2:], symbol[:2]),
                "p": "1", "ps": "200", "sr": "-1",
                "st": "REPORT_DATE", "source": "HSF10", "client": "PC",
                "v": "07306678536291241",
            }
            body = self._request(url, params)
            result = body.get("result")
            rows = [] if result is None else (result.get("data") or [])
            dates = self._bounded_dates(
                item.get("REPORT_DATE") for item in rows)
            self.counters["catalog_report_periods"] += len(rows)
            self.counters["selected_report_periods"] += len(dates)
            self.delisted_dates[symbol] = dates
        return self.delisted_dates[symbol]

    def _delisted_frame(self, symbol, statement_type):
        dates = self._delisted_catalog(symbol)
        if not dates:
            return pd.DataFrame()
        dataset, style = DELISTED_ENDPOINTS[statement_type]
        url = "https://datacenter.eastmoney.com/securities/api/data/get"
        quoted = ",".join("'{}'".format(value) for value in dates)
        params = {
            "type": dataset, "sty": style,
            "filter": ('(SECUCODE="{}.{}")'.format(
                symbol[2:], symbol[:2]) +
                "(REPORT_DATE in ({}))".format(quoted)),
            "p": "1", "ps": "200", "sr": "-1", "st": "REPORT_DATE",
            "source": "HSF10", "client": "PC", "v": "05767841728614413",
        }
        body = self._request(url, params)
        result = body.get("result")
        rows = [] if result is None else (result.get("data") or [])
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame.sort_values("REPORT_DATE", ascending=False,
                              inplace=True, ignore_index=True)
        return frame

    def statement_frame(self, symbol, statement_type, is_delisted):
        if statement_type not in LISTED_ENDPOINTS:
            raise ValueError("unsupported statement type")
        self.counters["statement_calls"] += 1
        if is_delisted:
            return self._delisted_frame(symbol, statement_type)
        return self._listed_frame(symbol, statement_type)


def load_bounded_eastmoney_adapters(config, session=None, sleeper=time.sleep):
    client = BoundedEastmoneyStatementClient(
        config["report_period_start_date"], config["history_end_date"],
        timeout=config.get("request_timeout_seconds", 30),
        max_attempts=config.get("request_max_attempts", 3),
        retry_backoffs=config.get("request_retry_backoff_seconds", (2, 10)),
        session=session, sleeper=sleeper)
    return client.adapters()
