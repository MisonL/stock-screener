"""扫硬编码凭据与私网 IP。

**不用一条大正则**：版本号 `10.4.3` 和私网段 `10.0.0.1` 在文本上分不开，
再收紧的边界也会误报或漏报。这里按类型分别判定：
  - 私网 IP 用 ipaddress 模块解析，天然不会把版本号当 IP；
  - 令牌用各自的前缀 + 精确长度/字符集。
"""
import ipaddress
import re
import sys

# 私网/保留段：用 ipaddress 判，不写正则
IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

#: **按「本项目的密钥名」匹配赋值**。
#:
#: 原来的规则全是"窄前缀 + 精确长度"（`ghp_`/`sk-`/`AKIA`…），对**本项目自己的**
#: 凭据一个都抓不到。实测（评审用本脚本跑的）：
#:
#:     NTFY_TOKEN=tk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6      → 未命中
#:     export SS_WEB_PASS=Hunter2MySecret                  → 未命中
#:     WESTOCK_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefg   → 未命中
#:     password: "s3cr3t-very-long-password"               → 未命中
#:
#: 而 RUNBOOK 里实际生成的就是 `tk_...`，`ntfy_` 那条规则**根本对不上**。
#: CI 的"敏感信息"门禁因此给的是假安全感。
SECRET_NAMES = re.compile(
    # **键名可以带引号**（`"SS_WEB_PASS": "..."` 是 JSON/YAML 常见写法，
    # Codex 评审指出原来的正则漏了这一整类）。
    r"[\"\']?(?:SS_WEB_PASS|SS_WEB_USER|NTFY_TOKEN|WESTOCK_ACCESS_TOKEN|"
    r"DEEPSEEK_API_KEY|OPENAI_API_KEY|LLM_API_KEY|API_KEY|SECRET|PASSWORD|PASSWD)"
    r"[\"\']?\s*[:=]\s*['\"]?([^\s'\"#]{6,})",
    re.IGNORECASE,
)

#: 赋值右侧出现这些就**不算泄漏**：变量引用、占位符、明显的示例值。
_NOT_A_SECRET = re.compile(
    r"^(?:\$|\{|<|%|\*{2,}|x{4,}|\*+|changeme|placeholder|your[-_]|example|\.\.\.)"
    r"|^\$[A-Za-z_]+$|^\$\{[^}]+\}$",
    re.IGNORECASE,
)


#: 这些是**值**而不是密钥：布尔/开关/常见标记。只按"整个值"匹配，
#: 不看子串——`on`/`true` 出现在长口令里是正常的。
_NOT_A_SECRET_WORDS = frozenset({
    "true", "false", "yes", "no", "on", "off", "none", "null", "nil",
    "enabled", "disabled", "required", "optional", "read", "write",
})


def _looks_like_secret_value(val: str) -> bool:
    """右侧这一串是不是真的像密钥。

    **不再要求"必须同时有数字和字母"**（Codex 评审抓到的漏检）：
    `correcthorsebatterystaple` 是很典型的口令写法，纯字母一样是凭据，
    而应用完全接受它。现在只做三件事：
      1. 排除占位符/变量引用（`$VAR` / `${VAR}` / `changeme` / `xxx` / `<...>`）；
      2. 排除整个值就是布尔/开关词的；
      3. 长度门槛（≥8，太短的不像口令）。
    代码形态不在这里判断——由 `_looks_like_code_line()` 在整行层面先筛掉。
    """
    if not val or _NOT_A_SECRET.search(val):
        return False
    if val.strip().lower() in _NOT_A_SECRET_WORDS:
        return False
    if len(val) < 8:
        return False
    # 反斜杠排除：`"TOKEN=abc\n"` 里的 `\n` 是**转义文本**，不是值的一部分。
    if "\\" in val:
        return False
    # **文档里的省略号/反引号/中文不是口令**：说明文字写 `KEY=tk_...`、
    # `KEY=值` 会被当成赋值。省略号、反引号、非 ASCII 一律排除。
    if "..." in val or "`" in val or not val.isascii():
        return False
    # **不再要求"含数字"**（Codex 第二轮）：纯字母口令同样会被应用接受，
    # 必须能报。代码形态改由**行级预筛**排除（见 `_looks_like_code_line`），
    # 不再靠"值里有没有括号/数字"去猜。
    return True


#: **带口令的连接串**：`scheme://user:password@host`。
#: 这类泄漏不会被"按密钥名赋值"的规则抓到（键名可能叫 `DB_CONN`/`DSN`/`URL`），
#: 但形式极其固定，直接按 URL 认。
_URL_CRED = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:([^\s@/]{3,})@")

# 各家令牌的固定前缀 + 长度，命中即算泄漏（无法从上下文解释成别的东西）
TOKENS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\btb_[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bntfy_[A-Za-z0-9]{24,}\b"),
    # **`tk_` 是 ntfy 真正生成的格式**（RUNBOOK §5 的 `ntfy token add` 输出），
    # 原来只有 `ntfy_` 那条，与实际格式对不上。
    re.compile(r"\btk_[A-Za-z0-9_-]{16,}\b"),
    # JWT（westock 的 access token 就是这种）
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]

# **不含 127.0.0.0/8**：`localhost:8000` / `127.0.0.1` 在文档和测试里是正常写法，
# 不是泄漏。要防的是「作者自己那台机器的内网地址」。
PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
)]


#: 整行出现这些就按**代码**处理，不走「密钥名赋值」规则。
#:
#: Codex 评审指出：靠"值里有没有数字/括号"区分代码与配置，既漏
#: （纯字母口令）又误报。**行级**判断准确得多——一旦出现调用/定义语法，
#: 整行就不是一条配置。
#: **不要放 `conn` / `None` 这种泛词**：`DB_CONN=postgres://...`、
#: `SS_NONE=...` 是**真配置**，会被整行误判成代码而漏检。
#: 真正需要的信号是"这行有调用/定义语法"——括号、`def `、`self.`、`->` 足够。
_CODE_LINE = re.compile(
    r"\(|\)|\bdef \b|\breturn \b|\blambda\b|\bself\.|->|"
    r"\bimport \b|\bclass \b|\bos\."
)


def _looks_like_code_line(line: str) -> bool:
    """这一行是代码而不是配置吗？"""
    return bool(_CODE_LINE.search(line))


def _inside_backticks(line: str, pos: int) -> bool:
    """`pos` 是否落在 markdown 的行内代码（`` `...` ``）里。

    **文档示例与真实配置必须分开**：CHANGELOG / RUNBOOK 里写
    `` `KEY=值` `` 是在**说明规则**，不是运维真的配了这个口令。
    但 CI 的历史扫描会把旧提交里的这类文本一起扫出来，于是
    「文档里解释一条规则」就会让门禁失败——第一次写这段说明时
    我就是这么把 CI 弄挂了（Codex 抓到）。
    """
    open_at = -1
    for i, ch in enumerate(line):
        if ch != "`":
            continue
        if open_at < 0:
            open_at = i
        else:
            if open_at < pos < i:
                return True
            open_at = -1
    return False


def find(line: str) -> list[str]:
    """返回这一行里命中的问题描述；空列表表示干净。"""
    hits = []
    for rx in TOKENS:
        if rx.search(line):
            hits.append(f"令牌 {rx.pattern[:24]}…")
    for m in _URL_CRED.finditer(line):
        # **同样要放过文档里的行内代码**：CHANGELOG 写 `` `scheme://user:password@host` ``
        # 是在说明规则，不是真的连接串（第一次提交就踩了）。
        if _inside_backticks(line, m.start()):
            continue
        if not _NOT_A_SECRET.search(m.group(1)):
            hits.append(f"连接串里带口令 {m.group(0)[:32]}")
    if not _looks_like_code_line(line):
        for m in SECRET_NAMES.finditer(line):
            if _inside_backticks(line, m.start()):
                continue                      # 文档里在解释规则，不是真配置
            if _looks_like_secret_value(m.group(1)):
                hits.append(f"疑似密钥赋值 {m.group(0)[:40]}")
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue                     # 版本号如 10.4.3 会在这里被拒
        if any(ip in net for net in PRIVATE_NETS):
            hits.append(f"私网地址 {ip}")
    return hits


def main() -> int:
    bad = 0
    for lineno, line in enumerate(sys.stdin, 1):
        for h in find(line):
            print(f"{lineno}: {h} :: {line.rstrip()[:160]}")
            bad += 1
    if bad:
        print(f"\n共 {bad} 处", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
