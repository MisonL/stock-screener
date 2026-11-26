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
    r"(?:SS_WEB_PASS|SS_WEB_USER|NTFY_TOKEN|WESTOCK_ACCESS_TOKEN|"
    r"DEEPSEEK_API_KEY|OPENAI_API_KEY|LLM_API_KEY|API_KEY|SECRET|PASSWORD|PASSWD)"
    r"\s*[:=]\s*['\"]?([^\s'\"#]{6,})",
    re.IGNORECASE,
)

#: 赋值右侧出现这些就**不算泄漏**：变量引用、占位符、明显的示例值。
_NOT_A_SECRET = re.compile(
    r"^(?:\$|\{|<|%|\*{2,}|x{4,}|\*+|changeme|placeholder|your[-_]|example|\.\.\.)"
    r"|^\$[A-Za-z_]+$|^\$\{[^}]+\}$",
    re.IGNORECASE,
)


def _looks_like_secret_value(val: str) -> bool:
    """右侧这一串是不是真的像密钥。

    要求**至少含一个数字和一个字母**（排除 `true`/`on`/`none` 这类布尔值）
    或长度足够长；同时排除 `$VAR` / `${VAR}` / `changeme` / `xxx` 这类占位。
    """
    if not val or _NOT_A_SECRET.search(val):
        return False
    if len(val) < 8:
        return False
    has_digit = any(c.isdigit() for c in val)
    has_alpha = any(c.isalpha() for c in val)
    return has_digit and has_alpha


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


def find(line: str) -> list[str]:
    """返回这一行里命中的问题描述；空列表表示干净。"""
    hits = []
    for rx in TOKENS:
        if rx.search(line):
            hits.append(f"令牌 {rx.pattern[:24]}…")
    for m in SECRET_NAMES.finditer(line):
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
