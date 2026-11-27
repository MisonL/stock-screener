"""发布脚本的**行为**测试。

重点不是「脚本能跑」，而是它在该拒绝的时候真的拒绝：

- 版本不是语义化 → 拒
- 新版本不大于当前（改回去 / 原地打转）→ 拒
- `[Unreleased]` 只有分类标题、没有条目 → 拒（否则发出「什么都没变」的版本）
- **校验必须发生在写入之前**（先改了文件再报错，会留下半改的工作区）
- **tag 已存在必须在写盘前发现**（否则 commit 已做、tag 指向旧提交）

干跑路径用**临时目录副本**测：直接在仓库根跑 `--apply` 会改真文件、打真 tag。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"

FILES_TO_GUARD = ("core/version.py", "pyproject.toml", "package.json",
                  "web/package.json", "package-lock.json", "web/package-lock.json",
                  "CHANGELOG.md")


def _run(*args: str, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """跑**指定目录下**的 release.py。

    `ROOT` 由脚本自身的 `__file__` 推出，所以沙箱测试必须跑副本里的那份脚本，
    否则它改的仍是真仓库。
    """
    base = cwd or ROOT
    script = base / "scripts" / "release.py"
    return subprocess.run([sys.executable, str(script), *args],
                          cwd=base, capture_output=True, text=True, env=env)


#: 假 git 桩共用的头部：跳过 `-c k=v` 等全局选项，让 `$1` 真的是子命令。
#: release.py 为发版提交钉死身份（`git -c user.name=.. -c user.email=.. commit`），
#: 不看这一步的桩会把 `-c` 当子命令，"查 tag" 就永远返回"标签存在"。
SKIP_GLOBAL_OPTS =     'while [ $# -gt 0 ]; do\n' \
    '  case "$1" in\n' \
    '    -c|--git-dir|--work-tree|-C) shift 2 ;;\n' \
    '    -*) shift ;;\n' \
    '    *) break ;;\n' \
    '  esac\n' \
    'done\n'


def _apply_release_ok(tmp: Path):
    """在临时副本里备好 fake git 与有条目的 Unreleased，返回 (fakebin, gitlog)。"""
    fake = tmp / "fakebin"
    fake.mkdir()
    # 最小 git：记录调用并返回成功。
    # **必须跳过全局 `-c key=value` 选项**再找子命令：release.py 会为发版提交
    # 钉死身份（`git -c user.name=.. -c user.email=.. commit ...`），
    # 只看 `$1` 的桩会把 `-c` 当成子命令，"查 tag" 就永远返回"标签存在"。
    (fake / "git").write_text(
        '#!/bin/sh\n'
        '# 跳过全局选项：-c k=v / --git-dir=X 之类\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -c|--git-dir|--work-tree|-C) shift 2 ;;\n'
        '    -*) shift ;;\n'
        '    *) break ;;\n'
        '  esac\n'
        'done\n'
        '# rev-parse 是"查 tag 是否存在"，没有 tag 时 git 以 1 退出\n'
        'if [ "$1" = "rev-parse" ]; then exit 1; fi\n'
        'echo "git $@" >> "$FAKE_GIT_LOG"; exit 0\n', encoding="utf-8")
    (fake / "git").chmod(0o755)
    log = tmp / "git.log"
    log.write_text("", encoding="utf-8")
    (tmp / "CHANGELOG.md").write_text(
        "# 更新日志\n\n## [Unreleased]\n\n### 修复\n\n- 修了一个东西\n\n"
        "## [0.2.0] - 2026-01-01\n\n### 新增\n\n- 老功能\n", encoding="utf-8")
    return fake, log


def test_rejects_non_semver():
    out = _run("v1.2", "--apply")
    assert out.returncode != 0
    assert "不是语义化版本" in (out.stdout + out.stderr)


def test_rejects_version_not_greater_than_current():
    """只允许递增：把 0.2.0 改成 0.0.9 这种「降版」必须拒掉。"""
    out = _run("0.0.1", "--apply")
    assert out.returncode != 0
    assert "必须大于当前" in (out.stdout + out.stderr)


def test_rejects_same_version():
    from core.version import VERSION

    out = _run(VERSION, "--apply")
    assert out.returncode != 0
    assert "没有要发布的改动" in (out.stdout + out.stderr)


def test_dry_run_changes_nothing(release_sandbox):
    """不带 `--apply` 时绝不能改文件——干跑会改文件是最坏的一种「贴心」。

    在沙箱里跑（真仓库的 `[Unreleased]` 是空的，干跑本就该拒绝、走不到写入）。
    比对范围必须是**全部会被脚本触碰的文件**：旧版只查 3 个，
    `package.json` 被干跑改写后测试照样绿（复核实测的假绿）。
    """
    sandbox, fake, log = release_sandbox
    before = {p: (sandbox / p).read_text(encoding="utf-8") for p in FILES_TO_GUARD}
    out = _run("9.9.9", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode == 0, out.stderr
    assert "干跑结束" in out.stdout
    for p, text in before.items():
        assert (sandbox / p).read_text(encoding="utf-8") == text, f"{p} 被干跑改动了"


def test_dry_run_on_empty_unreleased_refuses_without_changes(release_sandbox):
    """干跑也必须拒绝空 `[Unreleased]`（不能在"只是看看"时放过无效发布）。

    **必须用沙箱**：旧版读的是真仓库，当时恰好 `[Unreleased]` 为空才绿；
    一旦有人往里加了条目（正常开发就会这样），这条立刻变成假红。
    测试不该依赖仓库此刻的状态。
    """
    sandbox, fake, log = release_sandbox
    (sandbox / "CHANGELOG.md").write_text(
        "# x\n\n## [Unreleased]\n\n### 新增\n\n## [0.2.0] - 2026-01-01\n",
        encoding="utf-8")
    before = {p: (sandbox / p).read_text(encoding="utf-8") for p in FILES_TO_GUARD}
    out = _run("9.9.9", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0 and "没有任何条目" in (out.stdout + out.stderr), out.stdout
    for p, text in before.items():
        assert (sandbox / p).read_text(encoding="utf-8") == text, f"{p} 被干跑改动了"


# ---- 纯函数：changelog 校验与下沉 ---------------------------------------

def _import_release():
    sys.path.insert(0, str(ROOT / "scripts"))
    import release
    return release


def test_changelog_with_only_headings_is_rejected():
    """`### 新增` 下面零条目也必须拒——只查 `^### ` 会放过这种「空版本」。"""
    rel = _import_release()
    text = ("# x\n\n## [Unreleased]\n\n### 新增\n\n### 修复\n\n"
            "## [0.2.0] - 2026-01-01\n")
    with pytest.raises(SystemExit) as exc:
        rel.new_changelog_text(text, "0.3.0", "2026-02-02")
    assert "没有任何条目" in str(exc.value)


def test_changelog_promotion_preserves_structure():
    """Unreleased 清空、新小节带**空行与日期**、旧内容保留。"""
    rel = _import_release()
    src = ("# 更新日志\n\n## [Unreleased]\n\n### 修复\n\n- 修了一个东西\n\n"
           "## [0.2.0] - 2026-01-01\n\n### 新增\n\n- 老功能\n")
    got = rel.new_changelog_text(src, "0.3.0", "2026-02-02")
    # 新版小节标题与它的第一个子标题之间要有空行（Markdown 规范）
    assert "## [0.3.0] - 2026-02-02\n\n### 修复" in got
    # 原 Unreleased 仍在且为空
    assert "## [Unreleased]\n\n## [0.3.0]" in got
    # 旧版本不能被吞
    assert "## [0.2.0] - 2026-01-01" in got and "老功能" in got


# ---- 写入时机与 git 前置条件（用临时副本 + fake git） ---------------------

@pytest.fixture
def release_sandbox(tmp_path):
    """把整个仓库复制到临时目录，用 fake git 替换，避免动真仓库。"""
    sandbox = tmp_path / "repo"
    shutil.copytree(ROOT, sandbox, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "node_modules", "web/dist", ".pytest_cache", "data"))
    fake, log = _apply_release_ok(sandbox)
    return sandbox, fake, log


def test_apply_failure_leaves_files_untouched(release_sandbox):
    """校验失败（如空 Unreleased）必须在**写盘之前**发生，一个文件都不能动。"""
    sandbox, fake, log = release_sandbox
    (sandbox / "CHANGELOG.md").write_text(
        "# x\n\n## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n", encoding="utf-8")
    before = {p: (sandbox / p).read_text(encoding="utf-8") for p in FILES_TO_GUARD}
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0 and "没有任何条目" in (out.stdout + out.stderr)
    for p, text in before.items():
        assert (sandbox / p).read_text(encoding="utf-8") == text, \
            f"{p} 在校验失败后被改动了（说明校验晚于写入）"


def test_existing_tag_blocks_before_any_write(release_sandbox):
    """tag 已存在：必须在写盘前拒绝，否则 commit 已做、tag 指向旧提交。"""
    sandbox, fake, log = release_sandbox
    # fake git：只有 rev-parse（查 tag）返回"已存在"，其余成功
    (fake / "git").write_text(
        '#!/bin/sh\n' + SKIP_GLOBAL_OPTS +        'if [ "$1" = "rev-parse" ]; then echo "abc123"; exit 0; fi\n'
        'echo "git $@" >> "$FAKE_GIT_LOG"; exit 0\n', encoding="utf-8")
    (fake / "git").chmod(0o755)
    before = {p: (sandbox / p).read_text(encoding="utf-8") for p in FILES_TO_GUARD}
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0 and "已存在" in (out.stdout + out.stderr)
    for p, text in before.items():
        assert (sandbox / p).read_text(encoding="utf-8") == text, \
            f"{p} 在 tag 冲突时被改动了（说明预检晚于写入）"


def test_commit_succeeded_but_tag_failed_leaves_consistent_state(release_sandbox):
    """commit 成功、tag 失败时**不能把文件还原成旧版**。

    旧回滚逻辑无条件恢复文件 + `git reset -q`：此时 HEAD 已经前进到 release
    提交，index 是新版本、工作区被还原成旧版本 → 工作区变脏，重跑被 dirty
    检查挡住，用户手里是「提交说新版、工作区说旧版」的错位状态。

    正确做法：commit 既成事实就别动文件，明确告诉用户补一个 tag 即可。
    """
    sandbox, fake, log = release_sandbox
    (fake / "git").write_text(
        '#!/bin/sh\n' + SKIP_GLOBAL_OPTS +        'if [ "$1" = "rev-parse" ]; then exit 1; fi\n'
        'if [ "$1" = "tag" ]; then echo "fatal: hook 拒绝" >&2; exit 1; fi\n'
        'echo "git $@" >> "$FAKE_GIT_LOG"; exit 0\n', encoding="utf-8")
    (fake / "git").chmod(0o755)
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0
    # 已经 commit 的新版本必须原样留在工作区（不能再被还原）
    assert 'VERSION = "0.3.0"' in (sandbox / "core" / "version.py").read_text(encoding="utf-8"), \
        "commit 成功后又把文件还原成旧版，会造成提交/工作区版本错位"
    # 报错必须给出可执行的补救办法，而不是裸 traceback
    assert "git tag" in (out.stdout + out.stderr), \
        "没有告诉用户如何补 tag"


def test_lockfile_versions_get_updated(release_sandbox):
    """lockfile 顶层与 packages[""] 的 version 都必须跟着走，否则 npm install 有噪声 diff。"""
    sandbox, fake, log = release_sandbox
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode == 0, (out.stdout + out.stderr)
    for name in ("package-lock.json", "web/package-lock.json"):
        before = json.loads((ROOT / name).read_text(encoding="utf-8"))
        after = json.loads((sandbox / name).read_text(encoding="utf-8"))
        own = {str(after.get("version")),
               str((after.get("packages") or {}).get("", {}).get("version"))}
        assert own == {"0.3.0"}, f"{name} 本包版本没跟上：{own}"
        # **依赖版本一个都不能动**：曾用「替换全部 version」把 235 个依赖版本
        # 全改成项目版本号（westock-data-clawhub@0.2.1），`npm ci` 直接断，
        # 镜像构建整条失败。这条断言就是那次事故的回归测试。
        dep_before = {k: v.get("version") for k, v in (before.get("packages") or {}).items() if k}
        dep_after = {k: v.get("version") for k, v in (after.get("packages") or {}).items() if k}
        assert dep_before == dep_after, (
            f"{name} 的依赖版本被改动了："
            f"{[(k, dep_before.get(k), dep_after.get(k)) for k in dep_before if dep_before.get(k) != dep_after.get(k)][:3]}")


def _real_path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def test_commit_failure_leaves_no_staged_residue(release_sandbox):
    """`git add` 成功、`commit` 失败时，暂存区也要清干净。

    只还原工作区会留下 `MM` 状态：index 里是新版本、工作区是旧版本。
    重跑被 dirty 检查挡住，而且下一次 `git commit -a` 可能把新版本误提交进去。
    （Codex 复核在隔离副本里复现过：七个发布文件全是 `MM`，cached diff 仍是新版本。）
    """
    sandbox, fake, log = release_sandbox
    original_version = (sandbox / "core" / "version.py").read_text(encoding="utf-8")
    (fake / "git").write_text(
        '#!/bin/sh\n' + SKIP_GLOBAL_OPTS +        'if [ "$1" = "rev-parse" ]; then exit 1; fi\n'
        'if [ "$1" = "add" ]; then echo "git $@" >> "$FAKE_GIT_LOG"; exit 0; fi\n'
        'if [ "$1" = "commit" ]; then echo "fatal: synthetic commit failure" >&2; exit 1; fi\n'
        'if [ "$1" = "reset" ]; then echo "git $@" >> "$FAKE_GIT_LOG"; exit 0; fi\n'
        'echo "git $@" >> "$FAKE_GIT_LOG"; exit 0\n', encoding="utf-8")
    (fake / "git").chmod(0o755)
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0
    # 版本文件必须恢复成**原来的内容**（不写死版本号：仓库会往前发版）
    assert (sandbox / "core" / "version.py").read_text(encoding="utf-8") == original_version
    # 而且必须显式撤销暂存
    calls = log.read_text(encoding="utf-8").splitlines()
    assert any(c.startswith("git reset") for c in calls), (
        f"commit 失败后没有撤销 `git add` 的暂存，会留下 index/工作区版本不一致：{calls}")


# ---------------------------------------------------------------------------
# 推送（2026-11-27）：默认就该推 —— "别忘了 push --tags" 四次都没记住
# ---------------------------------------------------------------------------

def test_apply_pushes_commit_and_tag(release_sandbox):
    """发版成功后**必须把 commit 和 tag 都推到远端**。

    背景：`v0.2.141/142/144/147` 四次 main CI 变红，全是「tag 打了没推」。
    脚本原来只做到本地 tag，最后印一句「别忘了 push --tags」——
    靠人记得的事，四次足以证明记不住。CI 那边报的是「没有 tag」，
    和真相之间差一整轮排查。

    判据落在**行为**上：git 调用序列里必须出现这两个 push，
    而不是"源码里有 push 这俩字"。
    """
    sandbox, fake, log = release_sandbox
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode == 0, f"发版失败：{out.stdout}{out.stderr}"
    calls = log.read_text(encoding="utf-8")
    assert "git push origin HEAD" in calls, \
        f"没有推送发版提交：\n{calls}"
    assert "git push origin v0.3.0" in calls, \
        f"没有推送 tag（这正是 CI 变红的原因）：\n{calls}"
    # **顺序**：先推 commit 再推 tag。反过来的话 tag 可能指向远端不存在的提交，
    # 远端会拒收，而错误信息是 "cannot update ref"，与发布八竿子打不着。
    assert calls.index("push origin HEAD") < calls.index("push origin v0.3.0"), \
        f"推送顺序错了（tag 必须后推）：\n{calls}"


def test_no_push_skips_push_and_says_ci_will_be_red(release_sandbox):
    """`--no-push` 要真的不推，并且**明确说清**不推的后果。"""
    sandbox, fake, log = release_sandbox
    out = _run("0.3.0", "--apply", "--no-push", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode == 0, f"{out.stdout}{out.stderr}"
    calls = log.read_text(encoding="utf-8")
    assert "push" not in calls, f"--no-push 却仍然推送了：\n{calls}"
    # 本地该做的还是要做完
    assert "git commit" in calls and "git tag" in calls, f"本地步骤缺失：\n{calls}"
    text = out.stdout + out.stderr
    assert "未推送" in text, "没告诉用户这次没有推送"
    assert "git push origin v0.3.0" in text, "没给补推命令"
    assert "红" in text, "没提醒不推送会让 CI 变红"


def test_push_failure_keeps_local_state_and_gives_next_step(release_sandbox):
    """推送失败时**不能回滚**本地 commit/tag，要说清补推命令。

    commit 与 tag 已经成立，回滚只会制造"提交说新版、远端还是旧版"的错位，
    而且 tag 一旦删掉再打，时间戳就变了。正确处理：保留，告知补推。
    """
    sandbox, fake, log = release_sandbox
    (fake / "git").write_text(
        '#!/bin/sh\n' + SKIP_GLOBAL_OPTS +
        'if [ "$1" = "rev-parse" ]; then exit 1; fi\n'
        'if [ "$1" = "push" ]; then echo "fatal: 网络不可达" >&2; exit 1; fi\n'
        'echo "git $@" >> "$FAKE_GIT_LOG"; exit 0\n', encoding="utf-8")
    (fake / "git").chmod(0o755)
    out = _run("0.3.0", "--apply", cwd=sandbox,
               env={"PATH": f"{fake}:{_real_path()}", "FAKE_GIT_LOG": str(log)})
    assert out.returncode != 0, "推送失败却返回成功"
    text = out.stdout + out.stderr
    assert "本地" in text and "补推" in text, f"没说清本地状态与下一步：\n{text}"
    assert "git push origin v0.3.0" in text, f"没给 tag 的补推命令：\n{text}"
    # 本地状态必须完好：版本已是新的，tag 步骤也执行过
    assert 'VERSION = "0.3.0"' in (sandbox / "core" / "version.py").read_text(encoding="utf-8"), \
        "推送失败后把已提交的新版本回滚了"
    calls = log.read_text(encoding="utf-8")
    assert "git tag" in calls, f"推送失败后 tag 也没了：\n{calls}"
