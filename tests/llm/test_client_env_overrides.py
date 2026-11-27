"""环境变量覆盖 LLM 端点/模型的口径。

2026-11-27 生产事故里暴露出来的两件事：
1. 站点 `.env` 改了 `LLM_MODEL`，**毫无效果**——代码根本没读这个变量，
   于是"换模型"只改了 yaml 才会生效，排查时极易误判成"已经换了"。
2. `LLM_BASE_URL=` 写成空串时，原实现（`os.environ.get(k, default)`）
   会返回空串而不是回退 yaml——所有请求打到相对地址上。

三个变量（key / base_url / model）必须**同一套语义**：
有非空值就用 env，否则回退 yaml。
"""

import pytest

from llm.client import LlmClient


class _Cfg:
    class llm:                                   # noqa: N801 - 故意做成属性
        base_url = "http://from-yaml:1234/v1"
        api_key_env = "DEEPSEEK_API_KEY"
        model = "model-from-yaml"


@pytest.mark.parametrize("env,expected", [
    (None, "http://from-yaml:1234/v1"),          # 没设 → yaml
    ("", "http://from-yaml:1234/v1"),            # 空串 → yaml（不是空串！）
    ("   ", "http://from-yaml:1234/v1"),         # 只有空白 → yaml
    ("http://from-env:9999/v1", "http://from-env:9999/v1"),
])
def test_base_url_env_falls_back_to_yaml_when_blank(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LLM_BASE_URL", env)
    assert LlmClient(_Cfg())._base_url == expected


@pytest.mark.parametrize("env,expected", [
    (None, "model-from-yaml"),
    ("", "model-from-yaml"),
    ("   ", "model-from-yaml"),
    ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
])
def test_model_env_is_actually_honoured(monkeypatch, env, expected):
    """`LLM_MODEL` 必须真的生效。

    原来代码里**完全没有**读这个变量，而站点 `.env` 里一直写着它。
    后果是"改了没反应"——2026-11-27 排查 LLM 403 时就卡在这里：
    换了 `.env` 里的模型，agent 依然报原来的错。
    """
    if env is None:
        monkeypatch.delenv("LLM_MODEL", raising=False)
    else:
        monkeypatch.setenv("LLM_MODEL", env)
    assert LlmClient(_Cfg()).model == expected


def test_model_env_and_yaml_agree_by_default(monkeypatch):
    """不设 env 时，模型必须来自 yaml（不能悄悄变成别的）。"""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    c = LlmClient(_Cfg())
    assert c.model == "model-from-yaml"
    assert c._base_url == "http://from-yaml:1234/v1"
