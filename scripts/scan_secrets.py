"""扫硬编码凭据与私网 IP。

**不用一条大正则**：版本号 `10.4.3` 和私网段 `10.0.0.1` 在文本上分不开，
再收紧的边界也会误报或漏报。这里按类型分别判定：
  - 私网 IP 用 ipaddress 模块解析，天然不会把版本号当 IP；
  - 令牌用各自的前缀 + 精确长度/字符集。
"""
import hashlib
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
#:     NTFY_TOKEN=tk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6      → 未命中  # ggignore
#:     export SS_WEB_PASS=Hunter2MySecret                  → 未命中  # ggignore
#:     WESTOCK_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefg   → 未命中  # ggignore
#:     password: "s3cr3t-very-long-password"               → 未命中  # ggignore
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


#: 完整的点分标识符：`ep.api_key`、`self._secret.value`。
#: **必须整串匹配**：带引号/空格/数字的值不会被它吞掉。
_DOTTED_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


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
    # **纯属性链不是密钥**（2026-11-27）：`api_key=ep.api_key` 这种
    # "把某个对象的属性赋给另一个参数"的写法，右边是代码不是字面量。
    # 只排除**完整的点分标识符**（`a.b` / `self._x.y`），不做更宽的模糊匹配——
    # 一轮评审里正因为放宽到"含 conn/None 就算代码"而漏掉了真配置（见 _CODE_LINE）。
    # 抓到的值常带尾部标点（`ep.api_key,` / `x.y;`）——先剥掉再整串比对，
    # 否则 `fullmatch` 永远不命中，这条例外等于没写。
    if _DOTTED_IDENT.fullmatch(val.strip().rstrip(" ,;:")):
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


#: **已知的"示例行"整行白名单**（逐字，去首尾空白后比对）。
#:
#: ## 为什么需要它
#:
#: CI 的密钥门禁原先是**按文件名排除**这两个文件的：
#:     扫自己必然误报。排除这两个文件；它们的行为由 tests/test_scan_secrets.py 保证。
#: 这条排除把**最容易被贴上真实凭据的两个地方**变成了扫描盲区 ——
#: 写规则要举例、写测试要造样本，人最容易顺手贴真值。`[实测]` 2026-11-26：
#: GitGuardian 报的正是这两份文件里的示例行（假值，误报），而**本仓库
#: 自己的门禁对它们是全盲的** —— 真凭据贴进同一个位置，CI 一声不响。
#:
#: ## 为什么是"整行内容"而不是"值"或"行号"
#:
#: · **行号**：文件一增删就漂移，且不表达"这行为什么是样本"；
#: · **值**：PEM 私钥头这种样本，它的"值"就是真凭据的首行 ——
#:   按值豁免等于给真私钥开后门；
#: · **整行**：样本行都带着 Python 字面量的引号/逗号或注释前缀
#:   （`"…",`、`#:     …`），与真实凭据行（裸值）形态不同。
#:   真凭据要与它逐字相同才会被豁免 —— 那种情况本身就不可能发生。
#:
#: ⚠️ 改这些样本行时白名单要跟着改，否则 CI 立刻报红。这是**故意的**：
#: 宁可让人重新确认一遍"这确实是个假样本"。
#:
#: ## 生成入口只认这几个文件（**这是安全边界，不是风格**）
#:
#: `_SAMPLE_FILES` 之外的文件**永远不会**被生成入口读入，因此
#: `api/auth.py` 里一条真凭据不可能被"顺手"哈希进来。
#: 生成入口自己跑那两条命令，**不接受 stdin** —— 早期版本让人把
#: `git ls-files | xargs cat` 的输出管进来，那等于把全仓库的命中行
#: 都交给它哈希：真凭据也会变成白名单条目（Sourcery 2026-11-26 指出）。
_SAMPLE_FILES = (
    "scripts/scan_secrets.py",
    "tests/test_scan_secrets.py",
    "CHANGELOG.md",
)
_SAMPLE_LINES = frozenset((
"0388ba798356b790f197fd36e526000d377cbc66186a7ac1f97b78c25209ffc7",
"0a5da4e15558101b5cf872cebbe7f8c6f5a5f6cd6f26230f4aab58058d04f747",
"1270d7dab59f90e3951fcd5792aafd6d9557445df8e9bb26d27a540478635c38",
"15ece51b8a528c5f2d6b3a6a8df08721a946420eae6f6f768292cf231ccfe607",
"356c70ae6f7cc405641fd6bac74ecee324c9d54ec10691a1c13b79c82b9a59a4",
"3a39b7460f3419283c5dffa60b53e39ed7e18025d60f62419ad4019816e28428",
"5198eedb51c64bce24635db3ddbf345ebf54840828516847c48d5444527aa4b6",
"53768c7198c8f505e3d06a60d525455a99f278f0bb2c07bd1d1cfcd49b6d2cca",
"587c015ff4140c542ab9780ea7782b31d2b8e9655374610590fbf064c8615530",
"58d056799140e7d14f83863244298258f6c39bc32b4f6c11a6d7ed2dafdcecb6",
"669db9ed027b9991172d71c367191940eb59c437549c1451e571342c5c247305",
"6a514a170471e5f79762b8a1f058c4283b690889d7b40bd941cf596689f8a668",
"7a16a721e32ea6d8d944b8a8e321b9c18c7289f915d7e30407a5eaf0d18b2959",
"8cdf81b353d06d0f314b29007526f752494c8333b730b2041476484cd84f70ec",
"93618051607e9f93bcb95a11e1ec4ddd3ec628db97b1be897bf822d80727fb65",
"a218d0060e64ad288e2f8d3802175dc4ce80ccf9d7cf957f9434aec6725e52b1",
"a58cd7cbd7ed4a975096f7fe6051712fb04e30d2597233345103a78cb22e59aa",
"a5eeab08c86d2141e436d995f8bc3d42f3f68f20ea18ddde3e3623e95a36b46c",
"aa76bb92567b1b109bc6bcfaa5fcbc82cdc8c38fbb6499185a5b4b9b7f1a50f4",
"c65a32ef5d72c2597f1de71baeeb23d6899503fde7c5e049715f862ca7373c30",
"ccb3db2a64c0778f7c1173c376d6fc88717a029ede3afc5f10f2e1283a3ad35e",
"cd22c89f6b6f41e8774773cc46a160076cc73b3ed803a3f6e536cce9d2bdfa5e",
"d6279ed88be69cc5adc446f1eaf933e0b6e8975ee7718b3d4ea8c4bb41c8c59f",
"da743dd33a99d9b650e98aecdb49eafc3ed0fa54863e315da8ce200e28d227dc",
"e6d4e68f19919a9a5c6dd3a54872f46c8d06a527eeb18297027ce80f99de9e1e",
"f4e578d1f0a7d4560e627cae5e179a7c281a057c60d3ccd8f1f60721b4eb6076",
"f8b284d57015807fa8e57c08e6f169a146059631c8e5977a6275092b378a51e0",
"fd950e8c04985c092ced1e13ec7f26b962b78622d2e96da5aa2ea800e2fdfd7f",
))


def _line_hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode("utf-8")).hexdigest()


def _is_sample(line: str) -> bool:
    """整行是已知示例行 → 豁免。

    ⚠️ 只用**整行逐字比对**，不做前缀/包含匹配 —— 包含匹配会让
    「示例行 + 真凭据」这种拼接行整行豁免。
    ⚠️ 白名单存的是 **sha256 摘要而非明文**：明文存进来，白名单自己就
    成了"含有凭据样本的文件"，扫描器报自己（实测第一次就是这么红的）。
    摘要既能让规则正常判，又不把示例串复制到第二个地方。
    重新生成：
        python3 scripts/scan_secrets.py --hash-lines
    它**自己**读 `_SAMPLE_FILES` 那三个文件 + 它们的 git 历史，
    输出直接贴回 `_SAMPLE_LINES`。**不要**再管 `git ls-files | xargs cat` ——
    那会把真凭据也哈希进来（Sourcery 2026-11-26 指出的那个洞）。
    """
    _s = line.strip()
    if _line_hash(_s) in _SAMPLE_LINES:
        return True
    # `git log -p` 的增删行带 `+`/`-` 前缀，先去掉一个再比 —— 否则历史扫描
    # 永远报"这些样本行曾经出现过"（实测：全历史扫描 23 处全是这个原因）。
    # ⚠️ 只去**一个**前缀字符：真凭据行去掉 `+` 后仍是真凭据，不会误豁免。
    if _s[:1] in "+-" and _line_hash(_s[1:]) in _SAMPLE_LINES:
        return True
    return False


def find(line: str) -> list[str]:
    """返回这一行里命中的问题描述；空列表表示干净。"""
    if _is_sample(line):
        return []
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


def _collect_sample_hashes(root, files, history_text=None) -> list[str]:
    """从**指定文件**里收集"已知示例行"的摘要（去重、排序）。

    ⚠️ 只读 `files` 列出的文件 —— 这是安全边界。传进来的文件之外，
    哪怕全仓库还有别的疑似凭据行，也不会被写成白名单条目。
    ⚠️ 调用前必须把 `_SAMPLE_LINES` 清空再扫，否则样本已被豁免、
    一条都收不到（早期版本踩过：输出为空还以为没命中）。
    """
    import pathlib

    seen: set[str] = set()

    def _add(_raw_lines):
        for _raw in _raw_lines:
            _t = _raw.strip()
            # `git log -p` 的增删行带 `+`/`-`，`_is_sample` 会剥掉一个；
            # 这里也剥，两种来源才能产出**同一个**摘要（否则历史扫描
            # 永远对不上白名单里的条目）。
            if _t[:1] in "+-":
                _t = _t[1:]
            if not _t or not find(_t):
                continue
            seen.add(_line_hash(_t))

    root = pathlib.Path(root)
    for _rel in files:
        _p = root / _rel
        if not _p.is_file():
            continue
        try:
            _add(_p.read_text(encoding="utf-8", errors="replace").split("\n"))
        except OSError:
            continue
    if history_text:
        _add(history_text.split("\n"))
    return sorted(seen)


def _print_sample_hashes() -> int:
    """维护模式：重新生成 `_SAMPLE_LINES`。

    **不接受 stdin**：早期版本让人把 `git ls-files | xargs cat` 的结果管进来，
    那会把**全仓库**的命中行（含真凭据）都哈希成白名单条目 —— 真凭据从此
    被门禁放过。现在只读 `_SAMPLE_FILES` 这三个文件，历史也限定
    `git log -- <这三个文件>`，越界的行**永远进不来**。
    """
    global _SAMPLE_LINES
    import pathlib
    import subprocess

    _keep = _SAMPLE_LINES
    _SAMPLE_LINES = frozenset()          # 关掉豁免，否则收不到样本行
    try:
        _root = pathlib.Path(__file__).resolve().parents[1]
        _hist = ""
        try:
            _hist = subprocess.run(
                ["git", "-C", str(_root), "log", "--all", "-p",
                 "--pretty=format:", "--", *_SAMPLE_FILES],
                capture_output=True, text=True, timeout=120,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            # 容器里没有 git（见 tests/conftest 的说明）—— 只出工作区部分，
            # 这是可接受的降级：白名单是**人工维护**的，缺历史条目会立刻
            # 在"扫描全部历史"那步报红，不会静默。
            _hist = ""
        _hashes = _collect_sample_hashes(_root, _SAMPLE_FILES, _hist)
    finally:
        _SAMPLE_LINES = _keep
    for _h in _hashes:
        print('    "%s",' % _h)
    print(f"[scan_secrets] {len(_hashes)} 条示例行摘要"
          f"（来源限定：{', '.join(_SAMPLE_FILES)}）", file=sys.stderr)
    return 0


def main() -> int:
    if "--hash-lines" in sys.argv[1:]:
        return _print_sample_hashes()
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
