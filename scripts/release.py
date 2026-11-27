#!/usr/bin/env python3
"""发布助手：**先全部校验，再统一写入**，最后才提交打 tag。

为什么是这个顺序：写入后校验意味着一旦校验失败，工作区已经被改了一半——
用户既没发成版，又被脏工作区挡住重跑。第十四轮复核实测到这一点
（`[Unreleased]` 为空时，四个版本文件已经被改成新版本）。

为什么默认干跑、且只认 `APPLY=1`：`APPLY=0` / `APPLY=false` 这种"显式关闭"
在 make 里是非空字符串、会被当成真值，实测会真的发布。宁可严格。

用法：

    python3 scripts/release.py 0.2.1            # 干跑，只打印将要做什么
    python3 scripts/release.py 0.2.1 --apply    # 真改、提交、打 tag **并推送**
    python3 scripts/release.py 0.2.1 --apply --no-push   # 同上但不推远端
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# 版本赋值行：容忍类型标注与前导空白。**不能只认行首无缩进**——
# 这种正则一旦不命中，旧版是静默不改（最坏），现在统一抛清晰错误。
VERSION_LINE = re.compile(r'^(\s*)VERSION(\s*:\s*str)?\s*=\s*"[^"]+"', re.M)
PRERELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")

# 版本号出现在这些文件里；lockfile 的顶层与 packages[""] 也要跟着走，
# 否则 `npm install` 会把它们改回来，产生莫名其妙的 diff。
JSON_VERSION_FILES = ("package.json", "web/package.json",
                      "package-lock.json", "web/package-lock.json")


class ReleaseError(SystemExit):
    """校验失败：打印人话后退出（不抛 traceback）。"""


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


# ---- 版本解析与比较 ------------------------------------------------------

def parse_version(v: str) -> tuple[int, int, int, tuple]:
    """拆成 `(major, minor, patch, pre)`；`pre` 为空 tuple 表示正式版。

    SemVer §11 的 precedence：同基版本下**预发布版小于正式版**
    （`0.2.0-rc1 < 0.2.0`）。旧实现只比前三段，把两者算相等，
    导致 `0.3.0-rc1 → 0.3.0` 这种正常晋级被拒。
    """
    m = PRERELEASE.match(v)
    if m is None:
        raise ReleaseError(f"无法解析版本号：{v}")
    major, minor, patch, pre = m.groups()
    pre_key: tuple = () if pre is None else tuple(
        (0, int(x)) if x.isdigit() else (1, x) for x in pre.split("."))
    return int(major), int(minor), int(patch), pre_key


def is_newer(new: str, current: str) -> bool:
    """`new` 是否严格大于 `current`（按 SemVer precedence）。

    正式版 > 同基预发布版：`pre` 为空 tuple 时，用一个"比任何预发布标识都大"
    的哨兵比较——空 tuple 天然小于任何非空 tuple，所以要显式翻转。
    """
    a = parse_version(new)
    b = parse_version(current)
    if a[:3] != b[:3]:
        return a[:3] > b[:3]
    a_pre, b_pre = a[3], b[3]
    if not a_pre and not b_pre:
        return False
    if not a_pre:                    # new 是正式版 → 更大
        return True
    if not b_pre:                    # current 是正式版 → new 更小
        return False
    return a_pre > b_pre


def read_current_version() -> str:
    text = _read(ROOT / "core" / "version.py")
    m = re.search(r'^\s*VERSION(?:\s*:\s*str)?\s*=\s*"([^"]+)"', text, re.M)
    if m is None:
        raise ReleaseError(
            "core/version.py 里找不到 `VERSION = \"x.y.z\"`（允许 `VERSION: str = ...`）。"
            "发布脚本必须能唯一确定当前版本。")
    return m.group(1)


# ---- 计算新内容（纯函数，不碰磁盘） --------------------------------------

def new_core_version_text(text: str, new: str) -> str:
    # VERSION_LINE 是已编译的 pattern，不能再传 flags
    updated, n = VERSION_LINE.subn(
        lambda m: f'{m.group(1)}VERSION{m.group(2) or ""} = "{new}"', text)
    if n != 1:
        raise ReleaseError(
            f"core/version.py 里 `VERSION = ...` 命中 {n} 次（预期 1 次）。"
            "无法确定该改哪一行，已中止（未写入任何文件）。")
    return updated


def new_pyproject_text(text: str, new: str) -> str:
    updated, n = re.subn(r'^(\s*)version(\s*)=\s*"[^"]+"', lambda m: f'{m.group(1)}version = "{new}"',
                         text, flags=re.M)
    if n != 1:
        raise ReleaseError(f"pyproject.toml 里 `version` 命中 {n} 次（预期 1 次），已中止")
    return updated


def new_json_text(text: str, new: str, *, lockfile: bool) -> str:
    """只改**本包自己的** version，绝不碰依赖版本。

    这里踩过一个很贵的坑：曾用正则替换 lockfile 里的**全部** version，
    替换 lockfile 里的全部 version，把 235 个依赖版本全改成了项目版本号
    （`westock-data-clawhub@0.2.1`），结果 `npm ci` 直接报
    "package.json and package-lock.json are not in sync"，镜像构建整条断掉。

    现在的做法：解析 JSON，只写两个位置（顶层 `version` 与 `packages[""] .version`），
    再序列化回文本，并**验证其余内容逐字节不变**——round-trip 万一改了格式
    （比如原文件是 4 空格缩进），宁可报错让人手工处理，也不要悄悄重排 235 行。
    """
    data = json.loads(text)
    if lockfile:
        own = (data.get("packages") or {}).get("")
        if not isinstance(own, dict) or "version" not in own:
            raise ReleaseError("lockfile 缺少 packages[\"\"].version，已中止")
        data["version"] = new
        own["version"] = new
    else:
        if "version" not in data:
            raise ReleaseError("package.json 里找不到顶层 \"version\" 字段，已中止")
        data["version"] = new

    updated = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # 保真校验：把新版本号**还原成原版本号**后，输出必须与原文逐字节相同。
    # 不相同说明这份文件不是 2 空格缩进风格，round-trip 会重排整个文件。
    original = json.loads(text)
    witness = json.loads(updated)
    if lockfile:
        witness["version"] = original.get("version")
        if isinstance((witness.get("packages") or {}).get(""), dict):
            witness["packages"][""]["version"] = \
                (original.get("packages") or {}).get("", {}).get("version")
    else:
        witness["version"] = original.get("version")
    if json.dumps(witness, ensure_ascii=False, indent=2) + "\n" != text:
        raise ReleaseError(
            "这份 JSON 的原始格式不是 2 空格缩进，重写会改动无关行（例如整份缩进）。"
            "已中止，请手工改 version 后重试。")
    return updated


def new_changelog_text(text: str, new: str, today: str) -> str:
    """把 `[Unreleased]` 下沉成 `[new] - today`，并留一个空的 `[Unreleased]`。"""
    m = re.search(r"^## \[Unreleased\]\s*$", text, re.M)
    if m is None:
        raise ReleaseError("CHANGELOG.md 缺少 `## [Unreleased]` 小节")
    head, rest = text[: m.start()], text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = (rest[: nxt.start()] if nxt else rest).strip("\n")
    tail = rest[nxt.start():] if nxt else ""

    # **"空"不能只看有没有 `###`**：写了 `### 新增` 但下面零条目，
    # 也会发出一个什么都没变的版本（复核实测）。要求至少一个列表项。
    if not re.search(r"^\s*[-*]\s+\S", body, re.M):
        raise ReleaseError(
            "`[Unreleased]` 下没有任何条目（只有分类标题也算空）。"
            "先写清楚这次改了什么再发布。")

    return (head + "## [Unreleased]\n\n" + f"## [{new}] - {today}\n\n{body}\n\n" + tail)


# ---- git ----------------------------------------------------------------

#: 发版提交的固定身份。**必须在这里钉死**：`release.py` 用的是当前 shell 的
#: git 配置，开发者本机通常配的是真名 + 真邮箱，于每跑一次发版就往历史里多写
#: 一个真实邮箱——推到公开仓库后永久可见、可被搜索引擎关联到本人。
#: 这不是假设：本仓库 12 个提交里有 5 个是作者的真实邮箱，全部来自这个脚本。
RELEASE_IDENTITY = ("stock-screener", "noreply@example.com")


def _git(*args: str, capture: bool = False) -> str:
    # `-c` 放在子命令**之前**，优先级高于仓库与全局配置
    env_prefix = ["env", "-u", "GIT_CONFIG_COUNT", "-u", "GIT_CONFIG_VALUE_0",
                  "-u", "GIT_CONFIG_VALUE_1", "git",
                  "-c", f"user.name={RELEASE_IDENTITY[0]}",
                  "-c", f"user.email={RELEASE_IDENTITY[1]}"]
    try:
        out = subprocess.run(env_prefix + list(args), cwd=ROOT, check=True,
                             capture_output=capture, text=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise ReleaseError(f"git {' '.join(args)} 失败：{detail}") from None
    return out.stdout if capture else ""


def tag_exists(tag: str) -> bool:
    env_prefix = ["env", "-u", "GIT_CONFIG_COUNT", "-u", "GIT_CONFIG_VALUE_0",
                  "-u", "GIT_CONFIG_VALUE_1", "git",
                  "-c", f"user.name={RELEASE_IDENTITY[0]}",
                  "-c", f"user.email={RELEASE_IDENTITY[1]}"]
    r = subprocess.run(env_prefix + ["rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="发布新版本")
    ap.add_argument("version", help="语义化版本号，如 0.2.1")
    ap.add_argument("--apply", action="store_true", help="真的改文件并提交/打 tag")
    ap.add_argument("--no-push", action="store_true", dest="no_push",
                    help="只在本地提交并打 tag，不推送（默认会推）")
    args = ap.parse_args(argv)

    new = args.version
    if not SEMVER.match(new):
        raise ReleaseError(f"不是语义化版本：{new}")

    current = read_current_version()
    if new == current:
        raise ReleaseError(f"当前已经是 {current}，没有要发布的改动")
    if not is_newer(new, current):
        raise ReleaseError(f"新版本 {new} 必须大于当前 {current}")

    # 项目其余时间一律 Asia/Shanghai；用本机 tz 的话跨零点发版会写错一天
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    tag = f"v{new}"

    print(f"当前 {current} → 目标 {new}（{today}）")
    print("将修改：" + "、".join(("core/version.py", "pyproject.toml") + JSON_VERSION_FILES
                                + ("CHANGELOG.md",)))
    print(f"将打 tag：{tag}")

    # ---- 阶段一：把所有新内容算出来（任何一步失败都不写盘）----
    contents: dict[Path, str] = {}
    contents[ROOT / "core" / "version.py"] = new_core_version_text(
        _read(ROOT / "core" / "version.py"), new)
    contents[ROOT / "pyproject.toml"] = new_pyproject_text(
        _read(ROOT / "pyproject.toml"), new)
    for name in JSON_VERSION_FILES:
        p = ROOT / name
        if not p.exists():
            continue
        contents[p] = new_json_text(_read(p), new, lockfile=name.endswith("lock.json"))
    contents[ROOT / "CHANGELOG.md"] = new_changelog_text(
        _read(ROOT / "CHANGELOG.md"), new, today)

    # ---- 阶段二：git 前置条件（在写盘之前查）----
    if not args.apply:
        print("\n干跑结束（未写任何文件）。加 --apply 真正执行。")
        return 0

    if tag_exists(tag):
        raise ReleaseError(f"tag {tag} 已存在。已中止（未改任何文件）。")
    dirty = _git("status", "--porcelain", capture=True).strip()
    if dirty:
        raise ReleaseError(f"工作区不干净，先提交或清理：\n{dirty}")

    # ---- 阶段三：统一写入 ----
    originals: dict[Path, str] = {p: _read(p) for p in contents}
    committed = False
    try:
        for p, text in contents.items():
            _write(p, text)
        _git("add", *[str(p.relative_to(ROOT)) for p in contents])
        _git("commit", "-q", "-m", f"chore(release): {tag}")
        committed = True
        _git("tag", "-a", tag, "-m", tag)
    except BaseException:
        if not committed:
            # commit 之前失败：把文件恢复原状，别留下"版本改了但没提交"的半套状态。
            for p, text in originals.items():
                _write(p, text)
            # **必须同时撤掉 `git add` 的暂存**：`add` 成功而 `commit` 失败时，
            # 只还原工作区会留下「index 里是新版本、工作区是旧版本」的 `MM` 状态，
            # 重跑被 dirty 检查挡住，还可能把新版本误提交进去。
            # 只 reset 本次涉及的文件：调用方若有别的已暂存内容不受影响
            # （入口的 dirty 检查本来也会拦下那种情况）。
            _git("reset", "-q", "--", *[str(p.relative_to(ROOT)) for p in contents])
            raise
        # **commit 已成功**：绝不能把文件还原成旧版——那样 HEAD 是新版、
        # 工作区是旧版，工作区变脏、重跑又被 dirty 检查挡住。
        # 留着一致的新版本，明确告诉用户补一个 tag 即可。
        _log = print
        _log(f"\ncommit 已生成，但打 tag 失败。文件保持新版本 {new} 不变。", file=sys.stderr)
        _log(f"修好原因后执行：git tag -a {tag} -m {tag}", file=sys.stderr)
        raise

    # ---- 阶段四：推送 ----
    #
    # **默认就推**，不再只印一句"别忘了 push --tags"。
    #
    # 为什么：tag 只存在本地时，CI 的版本门禁必红。v0.2.141 / 142 / 144 / 147
    # 四次 main 变红**全是这个原因** —— tag 早就在本地打好了，只是 push 没带
    # `--tags`。而 CI 报的是「没有 tag」，和真相之间差一整轮排查。
    # 靠人记得的事，迟早会漏；四次已经足够说明"记得住"这条路不通。
    #
    # 用 `HEAD` 而不是分支名：查分支名要额外跑 `rev-parse`，而它在各种
    # 测试桩/非常规状态下都可能失败，把一条简单的推送变成新的失败点。
    if args.no_push:
        print(f"\n已提交并打 tag {tag}（`--no-push`：**未推送**）。")
        print(f"⚠️  不推送的话 CI 的版本门禁会红。上线还需：")
        print(f"      git push origin HEAD")
        print(f"      git push origin {tag}")
        return 0

    try:
        _git("push", "origin", "HEAD")
        _git("push", "origin", tag)
    except ReleaseError as e:
        # commit 与 tag 都已在本地生成 —— 状态是完好的，**不要重置**。
        # 这里只补上"接下来该做什么"，而不是抛 traceback。
        print(f"\n⚠️  commit 与 tag 已在**本地**生成，但推送失败：{e}", file=sys.stderr)
        print("    本地状态是完好的（别 reset、别重跑）。修好网络/权限后补推：",
              file=sys.stderr)
        print("      git push origin HEAD", file=sys.stderr)
        print(f"      git push origin {tag}", file=sys.stderr)
        print("    ⚠️  只推了 commit 没推 tag，版本门禁同样会红。", file=sys.stderr)
        return 1

    print(f"\n已提交、打 tag 并推送 {tag}。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
