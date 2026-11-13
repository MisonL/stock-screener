"""Task 10：westock CLI 包装（除权事件主源）。"""
import json
import subprocess

import pytest

from core.datasource.westock import WestockCli
from core.http import UpstreamError, UpstreamTimeout


class FakeRunner:
    """记录命令行、按调用返回预设 JSON。"""

    def __init__(self, payload=None, returncode=0, stderr="", timeout=False):
        self.payload = payload if payload is not None else {"data": []}
        self.returncode = returncode
        self.stderr = stderr
        self.timeout = timeout
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self.timeout:
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, self.returncode, json.dumps(self.payload), self.stderr)


def _cli(payload, **kw):
    from core.datasource.westock import WestockCli

    return WestockCli(runner=FakeRunner(payload, **kw))


DIVIDEND = {"data": [
    {"exDiviDate": "2025-07-29", "cashDiviRMB": "39.74", "bonusShareRatio": "8", "tranAddShareRatio": "12"},
    {"exDiviDate": "2025-10-30", "cashDiviRMB": "24.90", "bonusShareRatio": "0", "tranAddShareRatio": "0"},
]}


def test_dividend_parses_per_10_shares():
    acts = _cli(DIVIDEND).dividend("sh600519", years=5)
    first = acts[0]
    assert (first.code, first.ex_date) == ("sh600519", 20250729)
    assert (first.cash_per_share, first.bonus_ratio, first.tran_ratio) == (3.974, 0.8, 1.2)
    assert first.source == "westock_dividend" and first.implied_factor is None


def test_command_shape():
    cli = _cli(DIVIDEND)
    cli.dividend("sh600519", years=20)
    args = cli.runner.calls[0]
    assert args[:3] == ["npx", "--no-install", "westock-data-clawhub"]
    assert args[3] == "dividend" and "sh600519" in args and "-y" not in args
    assert "--years" in args and args[args.index("--years") + 1] == "20" and "--json" in args


def test_same_ex_date_aggregated():
    payload = {"data": [
        {"exDiviDate": "2025-07-29", "cashDiviRMB": "39.74", "bonusShareRatio": "8", "tranAddShareRatio": "12"},
        {"exDiviDate": "2025-07-29", "cashDiviRMB": "1.00", "bonusShareRatio": "0", "tranAddShareRatio": "0"},
    ]}
    acts = _cli(payload).dividend("sh600519")
    assert len(acts) == 1
    assert acts[0].cash_per_share == pytest.approx(3.974 + 0.1)
    assert acts[0].bonus_ratio == 0.8 and acts[0].tran_ratio == 1.2


def test_timeout_is_upstream_timeout():
    with pytest.raises(UpstreamTimeout):
        _cli(DIVIDEND, timeout=True).dividend("sh600519")


def test_nonzero_exit_is_upstream_error():
    with pytest.raises(UpstreamError):
        _cli(DIVIDEND, returncode=1, stderr="boom").dividend("sh600519")


def test_bad_json_is_upstream_error():
    from core.datasource.westock import WestockCli

    runner = lambda args, **kw: subprocess.CompletedProcess(args, 0, "not json", "")  # noqa: E731
    with pytest.raises(UpstreamError):
        WestockCli(runner=runner).dividend("sh600519")


def test_batch_dividend_chunks_by_300():
    cli = _cli(DIVIDEND)
    codes = [f"sh6{i:05d}" for i in range(301)]
    out = cli.batch_dividend(codes)
    assert len(cli.runner.calls) == 2                 # 300 + 1，不是 500
    assert set(out) == set(codes)


def test_suspension_parses_dates():
    payload = {"data": [{"code": "sh600519", "suspendStart": "2025-01-02", "suspendEnd": "2025-01-03"}]}
    assert _cli(payload).suspension("2025-01-01", "2025-01-31") == [("sh600519", 20250102, 20250103)]


@pytest.mark.parametrize("method,sub", [("chip", "chip"), ("notice", "notice"), ("fund_flow", "fund-flow")])
def test_passthrough_commands(method, sub):
    cli = _cli({"data": [{"x": 1}]})
    result = getattr(cli, method)("sh600519")
    assert result == [{"x": 1}]
    assert cli.runner.calls[0][3] == sub


def test_error_json_raises_not_empty(tmp_path):
    """CLI 退出码 0 但输出 {'error': ...} 时必须抛错——否则「上游挂了」被当成「没有分红」。"""
    from core.datasource.westock import WestockCli
    from core.http import UpstreamError

    class R:
        returncode = 0
        stderr = ""
        stdout = '{"error": "upstream unavailable"}'

    src = WestockCli(runner=lambda *a, **k: R())
    with pytest.raises(UpstreamError):
        src.dividend("sh600519")


def test_missing_data_key_raises():
    from core.datasource.westock import WestockCli
    from core.http import UpstreamError

    class R:
        returncode = 0
        stderr = ""
        stdout = '{"status": "ok"}'

    src = WestockCli(runner=lambda *a, **k: R())
    with pytest.raises(UpstreamError):
        src.dividend("sh600519")


def test_data_null_elements_raise():
    class R:
        returncode = 0
        stderr = ""
        stdout = '{"data": [null]}'

    src = WestockCli(runner=lambda *a, **k: R())
    with pytest.raises(UpstreamError):
        src.dividend("sh600519")
