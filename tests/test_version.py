"""版本号的**一致性守卫**。

这些测试的价值不在「跑通」，而在「漂移就红」——每条都用变异验证过：

- 版本文本散在 `core/version.py`、`pyproject.toml`、两份 `package.json`、
  两份 lockfile 里。改一处忘改另一处，运行时与包元数据就会说两套话。
- git tag 与 `CHANGELOG.md` 的小节也必须对得上：只有 `[0.2.0]` 小节却没有
  `v0.2.0` tag（或反之），就是「有记录没落点」——事故时找不到可切回的版本。

守卫一律**读真实文件/真实 git**，不做 mock：mock 掉读取等于把被测对象换成常量。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.version import VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m is not None, "pyproject.toml 里找不到 version"
    return m.group(1)


def _json_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def _git_dir() -> Path:
    """定位 `.git`（工作区里它可能是文件，内容是 `gitdir: ...`）。"""
    p = ROOT / ".git"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                return (ROOT / line.split(":", 1)[1].strip()).resolve()
        raise AssertionError(".git 是文件但没有 gitdir 行")
    return p


def _tags() -> set[str]:
    """读 tag 列表。**不走 `git` 子进程**：dev 镜像里没有 git 二进制，
    守卫必须在测试容器里也能跑，否则 CI 里要么 error 要么全 skip，等于没守卫。
    直接读 `.git/refs/tags/` 与 `.git/packed-refs`（打了 tag 后通常在后者的也有）。
    """
    gd = _git_dir()
    tags: set[str] = set()
    refs = gd / "refs" / "tags"
    if refs.is_dir():
        for f in refs.rglob("*"):
            if f.is_file():
                tags.add(f.relative_to(refs).as_posix())
    packed = gd / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].startswith("refs/tags/"):
                tags.add(parts[1][len("refs/tags/"):])
    return tags


def test_version_is_semver():
    assert SEMVER.match(VERSION), f"VERSION 不是语义化版本：{VERSION}"


def test_all_manifests_agree_with_core_version():
    """所有包清单（含 lockfile 里本包自己的两处）必须与 `core/version.py` 一致。"""
    drifted: dict[str, object] = {}
    for name in ("package.json", "web/package.json"):
        got = _json_version(ROOT / name)
        if got != VERSION:
            drifted[name] = got
    for name in ("package-lock.json", "web/package-lock.json"):
        p = ROOT / name
        if not p.exists():
            continue
        # lockfile 里还有依赖自己的 version，只看本包：顶层 + packages[""]
        data = json.loads(p.read_text(encoding="utf-8"))
        own = {str(data.get("version")),
               str((data.get("packages") or {}).get("", {}).get("version"))}
        if own != {VERSION}:
            drifted[name] = sorted(own)
    pyproj = _pyproject_version()
    if pyproj != VERSION:
        drifted["pyproject.toml"] = pyproj
    assert not drifted, (
        f"版本号漂移（core/version.py={VERSION}）：{drifted}。"
        "改版本时这些文件必须一起改，否则运行时与包元数据会说两套话。")


def test_changelog_has_non_empty_entry_for_current_version():
    """当前版本的小节必须存在**且有实际条目**。

    只匹配标题是不够的：把整节正文删空、只留 `## [x.y.z]` 标题，
    旧版守卫照样通过（复核实测的假绿）。
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^##\s*\[{re.escape(VERSION)}\]", changelog, re.M)
    assert m is not None, f"CHANGELOG.md 里没有 `## [{VERSION}]` 小节"
    rest = changelog[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = rest[: nxt.start()] if nxt else rest
    assert re.search(r"^\s*[-*]\s+\S", body, re.M), (
        f"`## [{VERSION}]` 小节里没有任何条目（只有标题不算记录）")


def test_changelog_unreleased_section_exists():
    """Keep a Changelog 约定：必须有 `[Unreleased]`，日常改动先堆这里。"""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^##\s*\[Unreleased\]", changelog, re.M), \
        "CHANGELOG.md 缺少 `## [Unreleased]` 小节"


#: tag 缺失时的排查提示。CI 只能看到**远端** tag，所以"没推"和"没打"
#: 在它眼里长得一样 —— 而前者发生过四次（v0.2.141/142/144/147），每次都要
#: 人或 agent 从头查一遍 CHANGELOG 才发现 tag 其实在本地躺着。
#: 把这条判断写进报错里，下一次不用再想一遍。
_TAG_MISSING_HINT = (
    "\n\n"
    "⚠️ CI 看到的是**远端**的 tag。先分清是哪一种：\n"
    "  · 本地 `git tag -l v{VERSION}` 能看到 → **打了但没推**，"
    "补推即可：`git push origin v{VERSION}`（或 `git push --tags`）；\n"
    "  · 本地也没有 → 真的忘了打："
    "`git tag -a v{VERSION} -m v{VERSION}` 再推。"
)


def test_git_tag_exists_for_current_version():
    """当前版本必须有对应的 git tag `v{VERSION}`。

    docstring 说了很久「tag 与 changelog 必须一致」，但实现从没读过 git——
    仓库当时正好处于「CHANGELOG 有 `## [0.1.0]`、却没有 `v0.1.0` tag」的
    状态，守卫全绿，RUNBOOK 的回滚命令因此指向一个不存在的 ref。
    """
    tags = _tags()
    assert f"v{VERSION}" in tags, (
        f"没有 tag `v{VERSION}`（现有：{sorted(tags) or '无'}）。"
        "发布必须同时打 tag，否则回滚没有落点。"
        + _TAG_MISSING_HINT.format(VERSION=VERSION))


def test_every_changelog_version_has_a_tag():
    """**公开之后**的每个版本小节都要有对应 tag——防「有记录没落点」。

    公开仓库的首个提交是全新历史，之前的版本号没有对应的 git 对象；
    `<!-- public-since: x.y.z -->` 标出这条分界线，它之前的记录仅作沿革保留。
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"<!--\s*public-since:\s*(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*-->",
                  changelog)
    public_since = m.group(1) if m else VERSION
    versions = re.findall(r"^##\s*\[(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\]", changelog, re.M)
    # 只查不早于 public_since 的版本（按元组比较，不做字符串比）
    def _key(v):
        core = v.split("-", 1)[0]
        return tuple(int(x) for x in core.split("."))
    relevant = [v for v in versions if _key(v) >= _key(public_since)]
    tags = _tags()
    missing = [v for v in relevant if f"v{v}" not in tags]
    assert not missing, (
        f"CHANGELOG 记了 {missing}，但没有对应 tag。"
        f"现有 tag：{sorted(tags) or '无'}。"
        + _TAG_MISSING_HINT.format(VERSION=missing[0]))
