"""`scripts/scan_secrets.py` 的规则守卫。

这条门禁的价值全在**规则对不对**上：
  - 漏报 = 真泄漏推上公开仓库，事后再删也留在别人的 fork 里；
  - 误报 = 门禁变噪音，几次之后就没人看了，等于没有。

所以正反两面都钉住：既要有「必须抓到」的样本，也要有「必须放过」的样本
（尤其是版本号 `10.4.3` 这种和私网地址长得一样的）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scan_secrets import find  # noqa: E402


@pytest.mark.parametrize("line", [
    "DB_HOST=172.16.81.4",
    "正在连 192.168.1.5 上的服务",
    "url = http://10.0.0.12:8080/api",
    "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "aws AKIAIOSFODNN7EXAMPLE",
    "key sk-" + "a" * 32,
    "-----BEGIN RSA PRIVATE KEY-----",
    "ntfy_" + "x" * 30,
    "tb_" + "y" * 30,
])
def test_real_leaks_are_caught(line):
    assert find(line), f"漏报：{line}"


@pytest.mark.parametrize("line", [
    '"version": "10.4.3"',          # 版本号，不是 IP——最容易被正则误伤
    '"lru-cache": "^10.4.2"',
    "resolved: https://registry.npmjs.org/x/-/x-10.0.0.tgz",
    "tqdm==4.70.1",
    "public_host: stock.example.com",
    "连接 127.0.0.1:8000 本机调试",   # 回环是正常写法
    "sha256:" + "0" * 64,
])
def test_normal_content_is_not_flagged(line):
    """这些是正常内容，报出来就是噪音——门禁一旦开始误报就没人看了。"""
    assert not find(line), f"误报：{line} → {find(line)}"


def test_versions_that_look_like_ips_are_distinguished():
    """`10.4.3` / `172.16.1` 这类版本号不能被当成私网地址。

    这是这条扫描器**唯一真正的难点**：单靠正则加边界做不对，
    必须用 ipaddress 解析。这条测试钉住那个实现选择。
    """
    # 注意 `10.0.0.0.0` **不在此列**：五段式在文本上确实会切出 `10.0.0.0`，
    # 而那是合法私网地址——这种写法本身就不会出现在版本号里，报出来是对的。
    for v in ("10.4.3", "172.16.1", "192.168.0", "1.2.3"):
        assert not find(f'version = "{v}"'), f"{v} 被误判成 IP"


def test_the_repository_itself_is_clean():
    """本仓库的已跟踪文件不该有任何命中。

    它会在每次改代码时重跑——如果谁不小心把内网地址写进配置，
    这里就红，而不是等推上去才发现。
    """
    # 不用 git：测试跑在容器里，容器内没有 git（见 conftest 的 skip 说明）。
    # 直接走文件系统，跳过 VCS/依赖/产物目录。
    # 跳过本地缓存/产物目录。`.firecrawl/` 是抓取工具的临时缓存（已在 .gitignore
    # 里），不属于仓库内容。
    # `.superpowers/` 是本地工作流缓存（已在 .gitignore 里、未入库），里面确实
    # 写着本机的内网地址——但它永远不会被推送，扫它只会制造噪音。
    SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", "dist", "data",
            ".tmp_smoke", ".venv", "htmlcov", ".firecrawl", ".superpowers"}
    bad = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP & set(p.relative_to(ROOT).parts):
            continue
        name = str(p.relative_to(ROOT))
        # 本文件和扫描器自己都包含「应该被抓到」的样本/规则文本，不参与扫描
        if name in ("tests/test_scan_secrets.py", "scripts/scan_secrets.py"):
            continue
        p = ROOT / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if find(line):
                bad.append(f"{name}:{i}: {line[:120]}")
    assert not bad, "仓库里检出疑似敏感信息：\n" + "\n".join(bad[:10])


def test_catches_this_projects_own_credential_formats():
    """**必须抓到本项目实际使用的凭据格式。**

    原来的规则全是"窄前缀 + 精确长度"（`ghp_`/`sk-`/`AKIA`…），
    对本项目自己的密钥一个都抓不到。实测（评审用本脚本跑的）四条全不命中——
    而 RUNBOOK 里实际生成的就是 `tk_...`，`ntfy_` 那条规则根本对不上。
    CI 的"敏感信息"门禁因此给的是假安全感。
    """
    from scripts.scan_secrets import find

    assert find("NTFY_TOKEN=tk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6")
    assert find("export SS_WEB_PASS=Hunter2MySecret")
    assert find("WESTOCK_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnopqrst")
    assert find('password: "s3cr3t-very-long-password"')


def test_does_not_flag_placeholders_and_variable_references():
    """**占位符/变量引用不能被当成泄漏**，否则门禁变成噪音没人看。"""
    from scripts.scan_secrets import find

    for line in [
        "SS_WEB_PASS=$SS_WEB_PASS",
        "NTFY_TOKEN=${NTFY_TOKEN}",
        "SS_WEB_PASS=changeme",
        "password: <your-password>",
        "SS_WEB_PASS=xxxxxxxx",
        'API_KEY=""',
        "SECRET_NAME=my-secret-name-here",   # 名字里含 secret，但值是标识符
    ]:
        assert find(line) == [], f"误报：{line} → {find(line)}"


def test_catches_passphrases_and_pure_numeric_passwords():
    """**纯字母口令和纯数字口令都要抓到**（Codex 评审抓到的漏检）。

    原来要求"必须同时有数字和字母"，于是 `correcthorsebatterystaple`
    （很典型的 passphrase）和 `12345678` 全部漏检——而应用完全接受它们。
    """
    from scripts.scan_secrets import find

    assert find("SS_WEB_PASS=correcthorsebatterystaple")
    assert find("SS_WEB_PASS=12345678")


def test_catches_quoted_json_style_keys():
    """**带引号的键名也要抓到**：`"SS_WEB_PASS": "..."` 是 JSON/YAML 常见写法。"""
    from scripts.scan_secrets import find

    assert find('"SS_WEB_PASS": "Abcdef12"')
    assert find("'NTFY_TOKEN': 'tk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6'")


def test_does_not_flag_code_expressions():
    """**代码形态一律不能误报**（放宽规则后实测误报 39 处）。

    `secret = ensure_secret(conn)`、`secret: bytes`、`api_key=self._api_key`
    是变量/函数调用，不是凭据。只看字符就能区分：出现括号/下标/逗号/分号
    就说明是表达式。
    """
    from scripts.scan_secrets import find

    for line in [
        "secret = ensure_secret(conn)",
        "def issue_session(secret: bytes, exp_ts: int) -> str:",
        "api_key = api_key",
        "self._api_key = api_key",
        "secret=SECRET, cfg=CFG, exp_ts=exp)",
        "secret = os.urandom(_SECRET_BYTES)",
    ]:
        assert find(line) == [], f"把代码当成凭据了：{line} → {find(line)}"


def test_catches_short_alpha_and_suffixed_passwords():
    """**Codex 第二轮指出的三类漏检**：短纯字母、带 `==`、带 `;`。

    应用完全接受这些口令，而第一版的"含 `()[]{},;=` 就排除"把它们全滤掉了。
    代码形态现在由**行级**预筛排除，不再靠值的字符去猜。
    """
    from scripts.scan_secrets import find

    assert find("SS_WEB_PASS=abcdefghij")
    assert find("SS_WEB_PASS=Abcdef12==")
    assert find("SS_WEB_PASS=Abcdef12;")


def test_backtick_quoted_doc_examples_are_not_config():
    """**文档里解释规则不算泄漏**，否则一写 CHANGELOG 就把 CI 弄挂。

    真实事故：我在 CHANGELOG 里举例说明"新规则能抓哪些口令"，把样本原样
    抄了进去 → 全仓扫描命中 2 处 → CI 红。历史扫描还会把这些文本一起扫出来，
    所以不能只靠"以后别这么写"。
    """
    from scripts.scan_secrets import find

    # markdown 行内代码：在解释规则
    assert find('漏掉了 `SS_WEB_PASS=correcthorsebatterystaple` 这种写法') == []
    assert find('- 新增 `"NTFY_TOKEN": "tk_a1b2c3"` 的识别') == []
    # 但**不在**反引号里的就是真配置，必须报
    assert find('SS_WEB_PASS=correcthorsebatterystaple')


def test_doc_placeholders_are_not_flagged():
    """文档里的省略号/中文占位不是口令。"""
    from scripts.scan_secrets import find

    assert find("把 `tk_xxx` 填进 NTFY_TOKEN=... 里") == []
    assert find("NTFY_TOKEN=值") == []
