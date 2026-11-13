"""真实上游响应样本：`*.json` 交给 json.load，其余按文本读。

样本文件名即 upstream 的名称（例如 `tencent_kline_sh600519.json`），
由各数据源 Task 往本目录投放，测试用 `load_fixture(name)` 读取。
"""
import json
from pathlib import Path

_DIR = Path(__file__).parent


def load_fixture(name: str) -> dict | str:
    p = _DIR / name
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text)
    return text
