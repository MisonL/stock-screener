"""westock CLI 包装：除权事件与停牌日历。

`dividend` 必须走 CLI 不能用 MCP（MCP 的 `data_dividend` 只回最近 1 年且忽略
`start`/`end`）；CLI `--years 20` 能回溯到 2013 年。

**单位口径**：CLI 的 `cashDiviRMB` / `bonusShareRatio` / `tranAddShareRatio` 都是
「每 10 股」的量（`10转12送8派39.74` → 39.74 / 8 / 12），入库前统一 ÷10 换成每股。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from core.http import UpstreamError, UpstreamTimeout

TIMEOUT_S = 15
_CHUNK = 300          # 不是 500：Node 子进程峰值内存 + Python 基线会撑爆 mem_limit=1g


@dataclass(frozen=True)
class CorpAction:
    """一条除权事件。`implied_factor` 只对 `source='snapshot_implied'` 有值。"""

    code: str
    ex_date: int
    cash_per_share: float
    bonus_ratio: float
    tran_ratio: float
    source: str = "westock_dividend"
    implied_factor: float | None = None


class WestockCli:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    # ---- 底层 -------------------------------------------------------------

    def _call(self, *args, timeout: int = TIMEOUT_S):
        cmd = ["npx", "--no-install", "westock-data-clawhub", *args, "--json"]
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise UpstreamTimeout(f"westock 超时 {timeout}s: {' '.join(cmd)}") from exc
        if proc.returncode != 0:
            raise UpstreamError(f"westock 退出码 {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise UpstreamError(f"westock 输出不是 JSON: {proc.stdout[:200]!r}") from exc

    @staticmethod
    def _rows(payload, context: str = "westock") -> list[dict]:
        """校验返回形状，**不把错误 JSON 当成空结果**。

        westock CLI 出错时退出码仍是 0、输出 `{"error": "..."}`——若直接
        `payload.get("data", [])` 就会得到 `[]`，把「上游挂了」伪装成「这只票没有
        分红记录」，除权检测静默漏掉全部公司行动（错得很安静）。
        """
        if isinstance(payload, dict):
            if payload.get("error") or payload.get("success") is False:
                raise UpstreamError(f"{context} 返回错误：{str(payload)[:200]}")
            if "data" in payload:
                rows = payload["data"]
            elif "rows" in payload:
                rows = payload["rows"]
            else:
                raise UpstreamError(f"{context} 返回里没有 data/rows：{str(payload)[:200]}")
        else:
            rows = payload
        if not isinstance(rows, list):
            raise UpstreamError(f"{context} 的数据不是数组：{type(rows).__name__}")
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise UpstreamError(f"{context} 的数据第 {i} 项不是对象：{type(row).__name__}")
        return rows

    # ---- 除权 -------------------------------------------------------------

    def dividend(self, code: str, years: int = 10) -> list[CorpAction]:
        rows = self._rows(self._call("dividend", code, "--years", str(years)), f"dividend {code}")
        return self._aggregate(code, rows)

    def batch_dividend(self, codes: list[str], years: int = 10, chunk: int = _CHUNK) -> dict[str, list[CorpAction]]:
        out: dict[str, list[CorpAction]] = {code: [] for code in codes}
        for start in range(0, len(codes), chunk):
            part = codes[start:start + chunk]
            rows = self._rows(self._call("dividend", "--codes", ",".join(part), "--years", str(years)))
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                code = str(row.get("code") or row.get("symbol") or "")
                if not code and len(part) == 1:
                    code = part[0]
                if code:
                    grouped.setdefault(code, []).append(row)
            for code, items in grouped.items():
                out[code] = self._aggregate(code, items)
        return out

    @staticmethod
    def _aggregate(code: str, rows: list[dict]) -> list[CorpAction]:
        """同一 `ex_date` 的多条记录先聚合（同日既派现又送转是常态）。"""
        agg: dict[int, list[float]] = {}
        for row in rows:
            ex_date = _ymd(row.get("exDiviDate") or row.get("ex_date"))
            if ex_date is None:
                continue
            cash, bonus, tran = agg.setdefault(ex_date, [0.0, 0.0, 0.0])
            agg[ex_date] = [
                cash + _per_share(row.get("cashDiviRMB")),
                bonus + _per_share(row.get("bonusShareRatio")),
                tran + _per_share(row.get("tranAddShareRatio")),
            ]
        return [
            CorpAction(code, ex_date, values[0], values[1], values[2])
            for ex_date, values in sorted(agg.items())
        ]

    # ---- 停牌 -------------------------------------------------------------

    def suspension(self, start: str, end: str) -> list[tuple[str, int, int]]:
        rows = self._rows(self._call("suspension", start, end))
        out = []
        for row in rows:
            code = str(row.get("code") or row.get("symbol") or "")
            begin = _ymd(row.get("suspendStart") or row.get("start"))
            finish = _ymd(row.get("suspendEnd") or row.get("end"))
            if code and begin and finish:
                out.append((code, begin, finish))
        return out

    # ---- 第二批工具用（第一批只透传，不落表） ------------------------------

    def chip(self, code: str):
        return self._rows(self._call("chip", code))

    def notice(self, code: str, n: int = 10):
        return self._rows(self._call("notice", code, "--n", str(n)))

    def fund_flow(self, code: str):
        return self._rows(self._call("fund-flow", code))

    def kline(self, code: str, period: str = "day", limit: int = 60) -> list[dict]:
        """批2 `get_sector_history` 主源（B5）：`kline <pt_code> --period day --limit N`。

        板块无除权，`--fq` 取 CLI 默认；返回原始行，形状归一化留给调用方（板块日线与个股不同源）。
        """
        return self._rows(self._call("kline", code, "--period", period, "--limit", str(limit)),
                           f"kline {code}")

    def search(self, keyword: str, stype: str = "sector") -> list[dict]:
        """关键词 → 代码（板块是 `pt…` 前缀）。模型只给中文板块名时用它归一。"""
        return self._rows(self._call("search", keyword, "--type", stype), f"search {keyword}")


def _per_share(value) -> float:
    """「每 10 股」→「每股」；空值/非数字按 0。"""
    try:
        return float(str(value).strip()) / 10.0
    except (TypeError, ValueError):
        return 0.0


def _ymd(value) -> int | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else None
