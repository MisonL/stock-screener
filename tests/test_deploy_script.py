"""`scripts/deploy.py` 的守卫：部署只能发**已提交**的内容。

曾经的事故：新加了 `api/logs.py` 却**没有提交**，而 `api/main.py` 已经 import 它，
`from api import logs`，然后用 `git ls-files | rsync` 部署——`ls-files` 只筛选
**文件名**、发的是**工作区内容**，于是 main.py 带着对新模块的引用上去了，
而新模块没上去。三个容器启动即 `ImportError`，站点 502 约 9 分钟。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# **测试容器里没有 `git`**（`pytest` 跑在 python:3.12-slim 里）：这两个用例要真调
# `git status` / 读脚本源码，只能在宿主机跑。`make test` 会带上它们。
HAS_GIT = shutil.which("git") is not None


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "deploy.py"), *args],
                          cwd=ROOT, capture_output=True, text=True)


@pytest.mark.skipif(not HAS_GIT, reason="容器内无 git，需在宿主机跑")
def test_dirty_workspace_is_refused():
    """工作区脏时**必须拒绝**（默认行为），并说明原因。

    容器内无 `git` 时跳过；对应的**静态检查**在
    `test_clean_check_precedes_any_remote_work` 里覆盖（不依赖 git 可执行）。
    """
    probe = ROOT / "scripts" / ".dirty_probe"
    probe.write_text("x", encoding="utf-8")
    try:
        out = _run("--dry-run")
        assert out.returncode != 0, f"脏工作区没有被拒绝：{out.stdout}"
        assert "工作区不干净" in out.stderr
        assert "git commit" in out.stderr, "拒绝时没告诉用户怎么办"
    finally:
        probe.unlink(missing_ok=True)


def test_clean_check_precedes_any_remote_work():
    """**静态**检查顺序：脏工作区检查必须发生在打包与任何 ssh/scp 之前。

    这条不依赖容器里有没有 `git`。事故的教训是「未提交的东西被部署了」，
    所以顺序是安全属性本身：先检查、后传输。
    """
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "check_clean(allow_dirty=" in src
    i_check = src.index("check_clean(allow_dirty=")
    # 打包与所有远端操作都必须排在脏检查之后
    for later in ('build_archive(tarball)', '"ssh"', '"scp"'):
        i_later = src.index(later)
        assert i_check < i_later, (
            f"{later} 出现在脏检查之前：脏工作区可能已被打包/上传。"
            "先检查、后传输才是安全顺序")


def test_uses_git_archive_not_ls_files():
    """打包必须来自提交对象（`git archive HEAD`），不是工作区。

    `git ls-files --files-from` 只筛文件名，内容是工作区的——这正是事故的成因。
    """
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "git\", \"archive\"" in src or '"archive"' in src, "没用 git archive"
    assert "'--format=tar.gz'" in src or '"--format=tar.gz"' in src or "--format=tar.gz" in src
    # 反向：不能再出现那种「筛文件名」的调用。
    # 文件头 docstring 里解释旧做法是允许的（那是文档），所以先剥掉 docstring 与注释，
    # 只查**可执行代码**里是否真在用。
    import ast

    body = ast.parse(src).body
    # `ast.get_docstring` 只接受 Module/Class/Function 节点，这里传 Module
    doc = ast.get_docstring(ast.parse(src)) or ""
    code = src.replace(doc, "")
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    assert "ls-files" not in code, \
        "可执行代码里还在用文件名筛选（它只筛文件名、内容是工作区的）"


def test_default_is_dry_run_safety():
    """不传 --dry-run 才会真部署；但 `--dry-run` 必须在打包后停下，不碰远端。"""
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--dry-run"' in src
    assert "到此为止" in src, "--dry-run 没有提前返回，会真的去连远端"


def test_no_destructive_rm_in_deploy_script():
    """部署脚本**绝不能**删目标目录。

    第一版写了 `rm -rf {dir}/*`——而 `data/market.db`（1.2 GB 生产库）、
    `data/backups/`、生成的 `cf/config.yml`、`config/*.env` 都不在 git 里，
    跑一次就把库和 7 份日备份全抹掉。`tar -x` 本身是覆盖语义，够了。

    扫的是**可执行代码**：要排除 (a) 模块 docstring（解释这段历史）、
    (b) 守卫函数自身（它里面是检查用的字面量，不是真要执行）。
    """
    import ast

    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    skip: set[int] = set()
    doc = ast.get_docstring(tree) or ""
    for ln_no, text in enumerate(lines, start=1):
        if text.strip() and text.strip() in doc:
            skip.add(ln_no)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_assert_no_destructive_command":
            for ln_no in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                skip.add(ln_no)

    offenders = []
    for ln_no, text in enumerate(lines, start=1):
        if ln_no in skip or text.lstrip().startswith("#"):
            continue
        for bad in ("rm -rf", "rm -fr"):
            if bad not in text:
                continue
            # **逐条**看每个 `rm -rf` 的目标，不能整行放过：
            # 同一行里可能既有合法的 `rm -rf "$bak"`，又有非法的 `rm -rf /srv/*`。
            for piece in text.split(bad)[1:]:
                target = piece.lstrip(" ;").split(";")[0].split("&&")[0].strip()
                # 允许：自己的临时备份目录（$bak）或 /tmp 下的一次性文件
                if target.startswith('"$bak"') or target.startswith("/tmp/"):
                    continue
                offenders.append(f"第 {ln_no} 行：{bad} {target[:60]}")

    assert not offenders, (
        "部署脚本的可执行代码里出现了删目录命令（目标含生产库与备份）：\n"
        + "\n".join(offenders))


def test_archive_extract_is_overwrite_not_wipe():
    """必须用 `tar -x`（覆盖）而不是先清空再解压。"""
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "tar -xzf" in src, "没有用 tar 解压"
    assert "mkdir -p" in src, "没有确保目标目录存在"


def test_preserve_uses_backup_restore_not_tar_skip_options():
    """保护目标机配置必须靠「备份 → 解压 → 恢复」，**不能给 tar 加跳过类选项**。

    两种错法都实机验证过：
      - `--exclude='config/notify.yaml'`：无条件匹配，首次部署这些文件一份不写入
      - `--skip-old-files`：跳过所有已存在文件，代码更新全部丢失
    """
    import ast

    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src)) or ""
    code = src.replace(doc, "")
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("--skip-old-files", "--exclude"):
        assert bad not in code, f"可执行代码里还有 {bad}：它会让部署丢失文件或变成空操作"
    assert "cp -p" in code and "tar -xzf" in code, "没有备份/恢复语义"


def test_backup_restore_semantics_on_real_tar(tmp_path):
    """真跑一次 tar，验证两种场景都对（不依赖任何 GNU tar 特有选项）。

    字符串断言只能证明「写了某个选项」，证明不了语义。审查与实机都证明过：
    只做字符串断言的守卫会让「部署变成空操作」这种问题带着全绿测试通过。
    """
    import subprocess

    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "config" / "notify.yaml").write_text("template\n", encoding="utf-8")
    (src / "main.py").write_text("code\n", encoding="utf-8")
    archive = tmp_path / "a.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(src), "."], check=True,
                   capture_output=True)

    def extract(dst: Path, backup: dict[str, str]) -> None:
        # 「备份 → 正常解压 → 恢复」——与 scripts/deploy.py 的远端 shell 同构
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(dst)], check=True,
                       capture_output=True)
        for rel, content in backup.items():
            (dst / rel).write_text(content, encoding="utf-8")

    # 场景 1：首次部署（目标为空）→ 配置必须照常写入
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    extract(fresh, {})
    assert (fresh / "config" / "notify.yaml").read_text(encoding="utf-8") == "template\n", \
        "首次部署时 config/notify.yaml 没有写入：容器会 ConfigError 起不来"

    # 场景 2：生产（已有真实配置）→ 保留配置，但代码要更新
    prod = tmp_path / "prod"
    (prod / "config").mkdir(parents=True)
    (prod / "config" / "notify.yaml").write_text("real-domain\n", encoding="utf-8")
    (prod / "main.py").write_text("OLD CODE\n", encoding="utf-8")
    extract(prod, {"config/notify.yaml": "real-domain\n"})
    assert (prod / "config" / "notify.yaml").read_text(encoding="utf-8") == "real-domain\n", \
        "生产配置被模板覆盖了"
    assert (prod / "main.py").read_text(encoding="utf-8") == "code\n", \
        "代码没有更新：部署变成空操作"


def test_build_failure_is_not_masked_by_tail():
    """构建必须 `set -o pipefail`，否则 `| tail -1` 会吞掉失败。

    `cmd | tail -1` 的退出码是 `tail` 的（几乎总是 0）。构建失败时脚本会继续
    拿**旧镜像**重建容器并报告"已重启"，故障被完全掩盖——比不部署更危险。
    """
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "set -o pipefail" in src, "远端构建没有 pipefail，失败会被 tail 吞掉"
    # pipefail 必须与 build 在**同一条** ssh 命令里
    line = next((ln for ln in src.splitlines() if "set -o pipefail" in ln), "")
    tail_block = src[src.index("set -o pipefail"):]
    assert "build api" in tail_block[:400], "pipefail 没有包住 build 命令"


def test_first_deploy_does_not_require_existing_database():
    """首次部署（目标机还没有 market.db）不能被前置检查挡住。"""
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    # `test -f .../market.db &&` 作为前置条件会让新建机器永远部署不了
    assert "test -f {args.dir}/data/market.db &&" not in src, (
        "market.db 存在性被当成前置条件：新建机器首次部署会直接中止")
    assert "首次部署" in src, "没有处理首次部署（无数据库）的分支"


def test_deploy_never_skips_code_updates():
    """部署**必须**更新代码，不能加任何会跳过已存在文件的 tar 选项。

    `--skip-old-files` 看起来只保护配置，实测它跳过**所有**已存在文件：
    线上部署后 `jobs/main.py` 仍带旧代码，一条刚修掉的假告警继续打，
    `docker logs` 骗过了我——部署脚本报告"已重启"，其实什么代码都没换。
    """
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    import ast

    doc = ast.get_docstring(ast.parse(src)) or ""
    code = src.replace(doc, "")
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    assert "--skip-old-files" not in code, (
        "还在用 --skip-old-files：它会跳过所有已存在文件，部署变成空操作")
    assert "--exclude" not in code, "--exclude 会让首次部署拿不到配置"
    # 必须是「备份 → 普通解压 → 恢复」三段
    assert "cp -p" in src and "tar -xzf" in src, "没有备份/恢复语义"
    assert 'bak=' in src, "没有备份目录"


def test_deploy_actually_updates_code_in_a_real_sandbox(tmp_path):
    """真跑一遍脚本逻辑：目标机已有旧代码时，解压后必须变成新代码。

    这条不依赖远端 SSH，只在本地模拟「备份 → 解压 → 恢复」，
    验证代码文件确实被覆盖、配置文件确实被保留。
    """
    import subprocess

    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "jobs").mkdir(parents=True)
    (src / "config" / "notify.yaml").write_text("template\n", encoding="utf-8")
    (src / "jobs" / "main.py").write_text("NEW CODE\n", encoding="utf-8")
    archive = tmp_path / "a.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(src), "."], check=True,
                   capture_output=True)

    dst = tmp_path / "dst"
    (dst / "config").mkdir(parents=True)
    (dst / "jobs").mkdir(parents=True)
    (dst / "config" / "notify.yaml").write_text("real-domain\n", encoding="utf-8")
    (dst / "jobs" / "main.py").write_text("OLD CODE\n", encoding="utf-8")

    keep = ("config/notify.yaml",)
    bak = tmp_path / "keep"
    bak.mkdir()
    for p in keep:
        target = bak / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((dst / p).read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(dst)], check=True,
                   capture_output=True)
    for p in keep:
        (dst / p).write_text((bak / p).read_text(encoding="utf-8"), encoding="utf-8")

    assert (dst / "jobs" / "main.py").read_text(encoding="utf-8") == "NEW CODE\n", \
        "代码没有被更新：部署是空操作"
    assert (dst / "config" / "notify.yaml").read_text(encoding="utf-8") == "real-domain\n", \
        "生产配置被覆盖了"


def test_pipefail_runs_under_explicit_bash():
    """`set -o pipefail` 必须跑在显式的 bash 里。

    SSH 登录 shell 可能是 dash，`set -o pipefail` 在 dash 下报
    `Illegal option -o pipefail` 并以 2 退出——整个部署在此中止。
    """
    src = (ROOT / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "bash -c " in src, "没用显式 bash -c：远端 dash 会让 pipefail 直接失败"
    assert "_sh_quote" in src, "远端脚本没有正确加引号"
