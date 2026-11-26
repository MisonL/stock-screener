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
        # ⚠️⚠️ 2026-11-26：**不再跳过扫描器和它自己的测试**。
        # 原先这里排除这两个文件（"含样本行，扫必然误报"），与 CI 的文件名
        # 排除是同一个洞 —— 而这两个文件恰恰最容易被顺手贴上真凭据。
        # 现在样本行由 `_SAMPLE_LINES` 精确豁免，文件照扫。
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

    assert find("NTFY_TOKEN=tk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6")  # ggignore
    assert find("export SS_WEB_PASS=Hunter2MySecret")  # ggignore
    assert find("WESTOCK_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnopqrst")  # ggignore
    assert find('password: "s3cr3t-very-long-password"')  # ggignore


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


def test_catches_connection_strings_with_inline_passwords():
    """**带口令的连接串**：键名可能是 `DB_CONN`/`DSN`/`URL`，不在密钥名列表里。

    这类泄漏只能用 URL 形式认：`scheme://user:password@host`。
    """
    from scripts.scan_secrets import find

    assert find("DB_CONN=postgres://app:Sup3rS3cret@db.internal:5432/stock")
    assert find("DSN=mysql://root:hunter2@127.0.0.1/x")


def test_code_line_regex_does_not_swallow_real_config_keys():
    """**`_CODE_LINE` 不能放 `conn`/`None` 这类泛词。**

    `DB_CONN=...`、`SS_NONE=...` 是真配置，放进去会让整行被当成代码而漏检。
    第一版就是这么写的，实测漏掉了 `DB_CONN=postgres://用户:口令@主机`。
    """
    from scripts import scan_secrets as ss

    assert not ss._looks_like_code_line("DB_CONN=postgres://a:bcd12345@h/db")
    assert not ss._looks_like_code_line("SS_NONE=abcdefghijk")
    assert not ss._looks_like_code_line("SS_WEB_PASS=abcdefghij")
    # 真正的代码形态仍要识别
    assert ss._looks_like_code_line("secret = ensure_secret(conn)")
    assert ss._looks_like_code_line("def issue_session(secret: bytes, exp_ts: int) -> str:")
# ---------------------------------------------------------------------------
# 门禁**不能把自己排除在外**（2026-11-26，GitGuardian 报出来的那次）
# ---------------------------------------------------------------------------
# 原 CI 是"按文件名跳过这两个文件"，理由是扫自己必然误报。代价是这两个
# 文件成了扫描盲区，而它们正是最容易顺手贴真凭据的地方（写规则要举例、
# 写测试要造样本）。GitGuardian 报的就是这两份文件里的假值样本。
# 下面这组用例把"文件照扫、只有已知样本行豁免"这条判据钉住。
#
# ⚠️ 本段**不写字面样本串**，一律用拼接构造：写了就又得给自己加白名单，
# 白名单里再写、再报 —— 无限自指（实测第一版就是这么红的）。

#: `tk_` 之后是一串顺序数字，真 token 不长这样
_TK = "tk_" + "a1b2c3d4e5f6g7h8i9j0" + "k1l2m3n4o5p6"
#: 与白名单里那行逐字一致的"示例行"（运行时拼出来，源码里没有这个字面量）
_SAMPLE_LINE = '    assert find("NTFY_TOKEN=%s")  # ggignore' % _TK


def _scanner_files():
    """两个曾经被整份排除的文件。"""
    return [ROOT / "scripts" / "scan_secrets.py",
            ROOT / "tests" / "test_scan_secrets.py"]


def test_sample_whitelist_covers_both_files():
    """两个曾经被排除的文件，逐行过扫描器必须**零命中**。

    ⚠️ 这条是"去掉文件级排除"的回归保护：只要有人在里面贴了新东西
    （真凭据或新样本），这里立刻红 —— 而不是等 GitGuardian 来报。
    """
    bad = []
    for f in _scanner_files():
        assert f.exists(), f"{f} 不存在"
        for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if find(line):
                bad.append(f"{f.name}:{i}: {find(line)}")
    assert bad == [], ("这两个文件里出现了未被白名单覆盖的疑似凭据：\n"
                       + "\n".join(bad))


def test_whitelist_is_exact_line_not_prefix():
    """白名单是**整行**比对，不是前缀/包含 —— 拼接一行真凭据必须被抓。

    若改成包含匹配，「示例行 + 真 token」这种拼接行会整行豁免，
    刚好给"顺手贴真值"留了门。
    """
    assert find(_SAMPLE_LINE) == [], "已知样本行应被豁免"
    _real = "tk_" + "z9y8x7w6v5u4t3s2r1q0p9o8" + "n7m6l5k4"
    assert find(_SAMPLE_LINE + " " + _real), \
        "样本行末尾拼接真 token 时必须报（白名单不能退化成前缀匹配）"


def test_whitelist_does_not_cover_real_looking_values():
    """与样本只差一个字符 → 必须抓（白名单不能按"值前缀"放宽）。"""
    assert find("NTFY_TOKEN=" + _TK[:-1] + "7"), \
        "只差最后一个字符的真值没被抓到 —— 白名单粒度太粗"
    # 裸的私钥头（真实凭据文件里就是这样一行）必须报，
    # 尽管样本行里带着引号逗号的那一版被豁免了。
    assert find("-----BEGIN " + "RSA PRIVATE KEY-----"), \
        "裸私钥头被样本行白名单误豁免了 —— 真私钥会漏过门禁"


def test_ci_secrets_job_scans_the_scanner_files():
    """CI 的 secrets job **必须把扫描器两个文件纳入扫描范围**。

    ⚠️ 不查"有没有这三种排除写法"（Sourcery 2026-11-26）：换一种
    grep / pathspec / 命令拼写就能绕过。直接断言 secrets job 的命令
    **没有**把这两个文件从输入里拿掉 —— 任何"排除它们"的写法都会让
    命令文本里出现这两个文件名作为排除目标。
    """
    import re
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # 取出 secrets job 的 run 块（从 `name: 敏感信息扫描` 到下一个顶层 job）
    m = re.search(r"name:\s*敏感信息扫描\n(.*?)(?=\n  [a-z]|$)", ci, re.S)
    assert m, "ci.yml 里找不到「敏感信息扫描」job"
    body = m.group(1)
    # 命令本身必须出现这两个文件的**正向引用**（`git ls-files` 不排除时，
    # 文件名不会出现在命令里；排除时才会出现）。所以判据是：
    #   · 命令文本里**没有**把这两个文件当排除目标；
    #   · 且 secrets job 确实在跑 `scripts/scan_secrets.py`。
    assert "scripts/scan_secrets.py" in body, "secrets job 没在跑扫描器"
    for _pat in (
        r"grep\s+-v[^\n]*scan_secrets",
        r":!scripts/scan_secrets\.py",
        r":!tests/test_scan_secrets\.py",
        r"--exclude[^\n]*scan_secrets",
        r"pathspec[^\n]*scan_secrets",
    ):
        assert not re.search(_pat, body), (
            f"secrets job 又在按文件名排除扫描器（命中 {_pat!r}）—— "
            f"那两个文件会重新变成盲区")


def test_hash_lines_mode_emits_digests_from_sample_files_only():
    """`--hash-lines` 必须**真正输出**示例行摘要，且只读 `_SAMPLE_FILES`。

    ⚠️ 不查"源码里有没有 `--hash-lines` 这几个字"（Sourcery 2026-11-26）：
    文档里也会出现，把处理器删成 no-op 照样绿。直接跑一次，断言：
      · 输出至少 1 条 64 位 hex；
      · 输出里的每一条都在当前 `_SAMPLE_LINES` 里（生成器与白名单一致）；
      · 它**不接受 stdin**：管一段真凭据进去，输出不变。
    """
    import hashlib
    import subprocess
    import sys
    from scripts.scan_secrets import _SAMPLE_FILES, _SAMPLE_LINES, _line_hash

    _py = sys.executable
    _script = str(ROOT / "scripts" / "scan_secrets.py")

    def _run(stdin_text=""):
        r = subprocess.run(
            [_py, _script, "--hash-lines"],
            input=stdin_text, capture_output=True, text=True, timeout=60,
            cwd=str(ROOT),
        )
        assert r.returncode == 0, f"--hash-lines 失败：{r.stderr}"
        return [l.strip().strip(',"') for l in r.stdout.splitlines()
                if l.strip().startswith('"')]

    out = _run()
    assert out, "--hash-lines 输出为空 —— 生成器坏了或样本文件读不到"
    for h in out:
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h), (
            f"输出不是 sha256 摘要：{h!r}")
        assert h in _SAMPLE_LINES, (
            f"生成器吐出了白名单里没有的摘要 {h} —— "
            f"要么白名单过时，要么生成器读到了 `_SAMPLE_FILES` 之外的文件")

    # 管一段**真凭据**进去：输出必须完全不变（证明不读 stdin）
    _poison = "NTFY_TOKEN=tk_" + "z" * 32 + "\n"
    out2 = _run(_poison)
    assert out2 == out, (
        "--hash-lines 读了 stdin —— 真凭据会被哈希进白名单"
        "（这正是 Sourcery 指出的那个洞）")

    # `_SAMPLE_FILES` 必须只覆盖那三个文件（安全边界本身）
    assert set(_SAMPLE_FILES) == {
        "scripts/scan_secrets.py",
        "tests/test_scan_secrets.py",
        "CHANGELOG.md",
    }, f"_SAMPLE_FILES 被扩大了：{_SAMPLE_FILES!r} —— 安全边界被拆掉"


def test_repo_clean_scan_does_not_skip_the_scanner_itself():
    """`test_the_repository_itself_is_clean` **不许**再把这两个文件排除掉。

    2026-11-26：CI 按文件名排除了 `scripts/scan_secrets.py` 与
    `tests/test_scan_secrets.py`，测试里也有同一份排除 —— 两层门禁同时
    对最容易被贴真凭据的两个文件失明。样本行现在由 `_SAMPLE_LINES`
    精确豁免，文件必须照扫。

    ⚠️ 要检查的字符串**拼接构造**：直接写字面量，这段源码本身就含该串，
    断言必然假红（与 `_SAMPLE_LINE` 同一个自指陷阱）。
    """
    src = (ROOT / "tests" / "test_scan_secrets.py").read_text(encoding="utf-8")
    _skip = ("if name in (" + '"tests/test_scan_secrets.py"'
             + ", " + '"scripts/scan_secrets.py")')
    assert _skip not in src, (
        "仓库自检又在按文件名跳过扫描器自己 —— 那两个文件会重新变成盲区")
