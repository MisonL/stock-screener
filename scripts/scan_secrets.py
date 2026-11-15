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

# 各家令牌的固定前缀 + 长度，命中即算泄漏（无法从上下文解释成别的东西）
TOKENS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\btb_[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bntfy_[A-Za-z0-9]{24,}\b"),
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
