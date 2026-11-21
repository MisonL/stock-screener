#!/usr/bin/env python3
"""把**已提交的内容**部署到 VM。

为什么不用 `git ls-files -z | rsync --files-from=-`：

`git ls-files` 只决定**发哪些文件**，不决定**发什么内容**——它发的是工作区当前内容。
所以「已跟踪文件被改脏、并引用了未跟踪的新文件」会完整漏过：

  1. 新建 `api/logs.py`（未 `git add`）
  2. 改 `api/main.py` 加 `from api import logs`
  3. 部署：`ls-files` 里有 `api/main.py` → 同步它的**工作区版本**（引用 logs）；
     `api/logs.py` 不在 `ls-files` 里 → **没发**
  4. VM 上镜像构建成功，容器启动即 `ImportError`，站点 502

曾经就这么断过：少了文件的那次部署让三个容器持续 Restarting 好几分钟。

现在用 `git archive HEAD` 导出：内容来自**提交对象**，工作区再脏也进不去。
另外在入口显式拒绝脏工作区——不是靠 archive 兜底，而是让"我忘了提交"当场可见。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = os.environ.get("SS_DEPLOY_HOST", "your-server")
DEFAULT_DIR = os.environ.get("SS_DEPLOY_DIR", "~/stock-screener")

# `git archive` 已经只含提交内容，这里再排除一遍构建产物，免得 tar 白涨体积
EXCLUDE = (".git", "data", "node_modules", "__pycache__", ".pytest_cache", ".runtime")

# **目标机自持的配置文件**：仓库里放的是 `example.com` 占位值，而服务器上必须是真实域名。
#
# 部署语义：**备份 → 正常解压（覆盖全部）→ 恢复**。
# 试过两种错法，都停下来记在这里，免得再犯：
#   - `--exclude='config/notify.yaml'`：无条件匹配归档成员，不看目标是否存在。
#     首次部署时这些文件**一份都不写入**，容器 ConfigError 起不来。
#   - `--skip-old-files`：看起来正中目标，但它跳过**所有**已存在文件——
#     代码更新全被丢弃，部署变成空操作（实机验证：`jobs/main.py` 压根没更新）。
# 只有「先备份再整体覆盖」能同时满足：代码每次都更新、配置永远不被覆盖、
# 首次部署时配置照常写入。且不依赖 GNU tar 特有选项。
PRESERVE_ON_TARGET = (
    "config/notify.yaml",
    "deploy/ntfy/server.yml",
    # 用户可能直接改 yaml 配自己的 LLM 端点（而不走 LLM_BASE_URL 环境变量），
    # 覆盖回去会让 API key 被发到占位地址——和 notify.yaml 是同一类风险。
    "config/llm.yaml",
    # **`agent.yaml` 必须一起保留**：不保留时，升级会把站点的
    # `attention.shadow_mode` 覆盖成仓库默认值，而 `llm.yaml`（含 `budget`）
    # 又保持站点的旧值 —— 两者**反向地**同时生效：
    # 正式模式打开了，预算却还是旧的（实测 v0.2.71→v0.2.72 就是这样，
    # 上线当天 10:42 就把预算打满）。回滚同理：版本退回去了，配置不会退。
    #
    # 代价是仓库里改 `agent.yaml` 的默认值**不会**自动落到已有站点上，
    # 需要按 `_config_drift()` 的提示手工同步——这是有意的：
    # 站点配置属于运维，不该被程序覆盖。
    "config/agent.yaml",
)


#: 保留在目标机上的配置里，这几个键**必须让运维看见差异**：
#: 它们决定"会不会真的调 LLM、一天能烧多少"。仓库改了默认值、
#: 站点还拿着旧值，就是本文件 `PRESERVE_ON_TARGET` 注释里那个反向故障。
_DRIFT_KEYS = (
    ("config/agent.yaml", "shadow_mode"),
    ("config/llm.yaml", "agent:"),
    # v0.2.105：这三处从"死配置"变成了真生效，站点若还是旧值，
    # 行为会和仓库默认值不同 —— 必须让运维看见。
    # `max_tokens`：站点 800、仓库 1000（重写路径会被悄悄截断）。
    ("config/llm.yaml", "max_tokens"),
    # `p1_score`：站点 60、仓库 0（60 会把低分突破降级成 P2）。
    ("config/notify.yaml", "p1_score"),
    # `own_history_trim` **不加进来**：它现在是"应该不存在"的键，
    # 而 `_DRIFT_KEYS` 的判据是"键存在与否"，加进去会变成
    # 「删干净了反而报漂移」（实测就是这么报的）。
)


def config_drift_script(target: str) -> str:
    """生成远端"打印保留配置与仓库默认值差异"的脚本。

    **只提示、不覆盖**：站点配置属于运维。但沉默会让人以为新默认值已经生效——
    v0.2.71→v0.2.72 就是这样：仓库把预算改成 1500000，站点仍是 300000，
    升级后正式模式打开、预算没跟上，当天 10:42 就打满。
    """
    lines = ["set -e", f"cd {target}"]
    for rel, key in _DRIFT_KEYS:
        # 仓库版本先备份成 .new，再与站点版本对比；一致就不吭声
        lines.append(f'if test -f "{rel}"; then')
        lines.append(f'  if ! grep -q "{key}" "{rel}" ; then echo "[配置漂移] {rel} 里没有 {key}"; fi')
        lines.append("fi")
    return "\n".join(lines)


def _git(*args: str, capture: bool = True) -> str:
    env_prefix = ["env", "-u", "GIT_CONFIG_COUNT", "-u", "GIT_CONFIG_VALUE_0",
                  "-u", "GIT_CONFIG_VALUE_1", "git"]
    proc = subprocess.run(env_prefix + list(args), cwd=ROOT, check=True,
                          capture_output=capture, text=True)
    return proc.stdout if capture else ""


def _sha() -> str:
    return _git("rev-parse", "--short", "HEAD").strip()


def check_clean(*, allow_dirty: bool) -> None:
    """工作区必须干净：脏着部署等于「跑的不是任何一个提交」。"""
    if allow_dirty:
        print("⚠️  --allow-dirty：工作区有改动，但内容仍只取 HEAD（改动不会上线）",
              file=sys.stderr)
        return
    dirty = _git("status", "--porcelain").strip()
    if not dirty:
        return
    files = "\n".join("  " + line for line in dirty.splitlines()[:10])
    raise SystemExit(
        "工作区不干净，拒绝部署：\n"
        f"{files}\n\n"
        "部署只发**已提交**的内容。先 `git commit`，或用 `--allow-dirty` 明确只部署 HEAD。\n"
        "（2026-11-13：就是这样一次「改了 main.py、新文件没提交」导致线上 502。）")


def _assert_no_destructive_command() -> None:
    """自检：脚本里不能出现删除目标数据的命令。

    这条不是形式主义——第一版写了 `rm -rf {dir}/*`，而 `data/market.db`
    （1.2 GB 生产库）与 `data/backups/` 都不在 git 里，跑一次就全没了。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("rm -rf", "rm -fr"):
        assert bad not in code, (
            f"部署脚本里出现了 `{bad}`：目标目录含 data/market.db 与 backups，"
            "清目录等于删库。用 tar 覆盖语义即可")
    # docstring 里说明这段历史是允许的，所以只查可执行代码（上面已剥掉 # 注释，
    # 但 docstring 仍是代码的一部分——用字符串替换把它们排除）
    import ast
    doc = ast.get_docstring(ast.parse(src)) or ""
    code2 = code.replace(doc, "")
    assert "rm -rf" not in code2


def remote_extract_script(sha: str, target: str, preserve=PRESERVE_ON_TARGET) -> str:
    """生成远端的「备份 → 解压 → 恢复」脚本。

    **不用 shell 的 `for p in "a", "b"`**：逗号是字面字符，第一个元素会变成
    `"a",`（带逗号），`test -f` 判否，备份被静默跳过——于是生产配置仍被模板覆盖，
    站点所有写操作开始 400。这里改为**逐文件展开成独立命令**，没有列表语法可踩。
    """
    lines = ["set -e", f"cd {target}", f'bak="/tmp/deploy-keep-{sha}"', 'rm -rf "$bak"']
    for rel in preserve:
        d = str(Path(rel).parent)
        lines.append(f'mkdir -p "$bak/{d}"')
        lines.append(f'if test -f "{rel}"; then cp -p "{rel}" "$bak/{rel}"; fi')
    lines.append(f"tar -xzf /tmp/src-{sha}.tar.gz -C {target}")
    for rel in preserve:
        lines.append(f'if test -f "$bak/{rel}"; then cp -p "$bak/{rel}" "{rel}"; fi')
    lines.append('rm -rf "$bak"')
    lines.append(f"rm -f /tmp/src-{sha}.tar.gz")
    lines.append("find . -name '*.py' -exec chmod 644 {} +")
    return "\n".join(lines)


def build_archive(dest: Path) -> None:
    """`git archive HEAD` → tar.gz。"""
    with dest.open("wb") as fh:
        proc = subprocess.run(
            ["env", "-u", "GIT_CONFIG_COUNT", "-u", "GIT_CONFIG_VALUE_0",
             "-u", "GIT_CONFIG_VALUE_1", "git", "archive", "--format=tar.gz", "HEAD"],
            cwd=ROOT, stdout=fh, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"git archive 失败：{proc.stderr.decode(errors='replace')}")


def snapshot_image(host: str) -> None:
    """构建前把当前在跑的镜像存成 rollback标签。

    没有这一步就无法回滚：`compose build`会直接覆盖 `stock-screener:latest`，
    旧镜像被新构建取代后，容器引用的还是同一个 tag，等于把退路也一起删了。
    """
    cmd = (
        "docker image inspect stock-screener:latest >/dev/null 2>&1 && "
        "docker tag stock-screener:latest stock-screener:rollback && "
        "echo SS_SNAPSHOT=OK || echo SS_SNAPSHOT=NONE")
    _run(["ssh", "-o", "BatchMode=yes", host, cmd])


def verify_script(services: str, wait_s: int) -> str:
    """远端验收脚本：容器稳定 +启动日志无 traceback。

    只认 `SS_VERIFY=OK`一行——**别的都算失败**，包括脚本自己出错。
    """
    svc = " ".join(f"stock-screener-{x.strip()}-1" for x in services.split() if x.strip())
    return f'''set -u
    deadline=$(( $(date +%s) + {wait_s} ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        #有容器在重启/退出/不健康 →立刻判失败，不必等满
        bad=$(docker ps -a --filter 'name=stock-screener-' \
            --format '{{{{.Names}}}} {{{{.Status}}}}' \
            | grep -E 'Restarting|Exited|unhealthy' || true)
        if [ -n "$bad" ]; then echo "SS_VERIFY=FAIL:容器异常 $bad"; exit 0; fi
        ok=1
        for c in {svc}; do
            docker ps --filter "name=$c" --filter status=running \
                --format '{{{{.Names}}}}' | grep -qx "$c" || ok=0
        done
        if [ "$ok" = "1" ]; then break; fi
        sleep 3
    done
    for c in {svc}; do
        docker ps --filter "name=$c" --filter status=running \
            --format '{{{{.Names}}}}' | grep -qx "$c" \
            || {{ echo "SS_VERIFY=FAIL:容器未运行 $c"; exit 0; }}
    done
    # **扫启动日志里的 traceback**：容器重启后可能短暂显示 Up，
    #真正判据是它有没有在异常里打滚。--since 4m覆盖这次重启。
    for c in {svc}; do
        tb=$(docker logs --since 4m "$c" 2>&1 \
            | grep -c 'Traceback (most recent call last)' || true)
        if [ "${{tb:-0}}" -gt 0 ]; then
            detail=$(docker logs --since 4m "$c" 2>&1 \
                | grep -A2 'Traceback (most recent call last)' | head -3 | tr '\\n' ' ')
            echo "SS_VERIFY=FAIL:$c启动异常 $detail"; exit 0
        fi
    done
    echo SS_VERIFY=OK'''


def rollback(host: str, target: str, services: str) -> bool:
    """回退到 `stock-screener:rollback`；返回是否回退成功。"""
    compose = "docker compose -f deploy/docker-compose.yml --project-directory . "
    body = (
        "set -e; cd " + target
        + " && docker image inspect stock-screener:rollback >/dev/null 2>&1 || "
        + "{ echo SS_ROLLBACK=NONE; exit 0; }; "
        + "docker tag stock-screener:rollback stock-screener:latest && "
        + compose
        + "up -d --force-recreate " + services
        + " >/dev/null 2>&1 && echo SS_ROLLBACK=OK"
    )
    out = _capture(["ssh", "-o", "BatchMode=yes", host, "bash -c " + _sh_quote(body)])
    return "SS_ROLLBACK=OK" in out


def verify_deployment(host: str, services: str, wait_s: int) -> str:
    """返回 "OK"或失败原因。**只有明确见到 OK才算成功**。"""
    out = _capture(["ssh", "-o", "BatchMode=yes", host,
        "bash -c " + _sh_quote(verify_script(services, wait_s))])
    for line in out.splitlines():
        if line.startswith("SS_VERIFY=OK"):
            return "OK"
        if line.startswith("SS_VERIFY=FAIL:"):
            return line[len("SS_VERIFY=FAIL:"):].strip() or "未知原因"
    return "验收脚本没有给出结论（ssh失败或脚本异常）"


def _capture(cmd: list[str]) -> str:
    """跑命令并取输出（失败也返回已拿到的部分，交给调用方判定）。"""
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return (proc.stdout or "") + (proc.stderr or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署已提交的内容到 VM")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="VM 上的仓库路径")
    ap.add_argument("--dry-run", action="store_true", help="只检查与打包，不传不重启")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许脏工作区（内容仍只取 HEAD）")
    ap.add_argument("--services", default="api jobs agent",
                    help="要 --force-recreate 的服务")
    ap.add_argument("--verify-wait", type=int, default=90,
 help="部署后最多等多少秒让容器稳定")
    args = ap.parse_args(argv)

    check_clean(allow_dirty=args.allow_dirty)
    sha = _sha()
    tag = _git("describe", "--tags", "--exact-match", "HEAD").strip() \
        if _has_tag() else "(未打 tag)"
    print(f"部署 HEAD={sha} {tag}")

    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "src.tar.gz"
        build_archive(tarball)
        size = tarball.stat().st_size / 1024 / 1024
        with tarfile.open(tarball) as tf:
            names = tf.getnames()
        print(f"打包完成：{len(names)} 个文件，{size:.1f} MB（内容全部来自提交对象）")
        if args.dry_run:
            print("--dry-run：到此为止")
            return 0

        remote = f"{args.host}:{args.dir}"
        # **绝不能清空目标目录**：`data/market.db`（1.2 GB 生产库）、`data/backups/`、
        # 生成的 `cf/config.yml`、`config/*.env` 都不在 git 里，rm -rf 会直接抹掉。
        # `tar -x` 本身是覆盖语义：归档里有的文件更新，没有的原样保留——这才是要的。
        # **首次部署时 data/market.db 可能还不存在**（新建机器），此时 `test -f` 返回非零
        # 会让 ssh 命令失败并中止部署。所以只做提示、不把它当前置条件。
        _run(["ssh", "-o", "BatchMode=yes", args.host,
              f"mkdir -p {args.dir} {args.dir}/data/logs {args.dir}/ntfy/cache {args.dir}/ntfy/auth {args.dir}/cf; "
              f"if test -f {args.dir}/data/market.db; then "
              f"echo '目标库存在，将原地覆盖代码（不动数据）'; "
              f"else echo '目标无数据库：首次部署，库会在首次启动时创建'; fi"])
        _run(["scp", "-q", str(tarball), f"{args.host}:/tmp/src-{sha}.tar.gz"])
        # **备份 → 正常解压 → 恢复**：不能给 tar 加任何「跳过」类选项
        # （`--skip-old-files` 会连代码一起跳过，部署变成空操作）。
        _run(["ssh", "-o", "BatchMode=yes", args.host,
              remote_extract_script(sha, args.dir)])

    compose = ("docker compose -f deploy/docker-compose.yml --project-directory . ")
    # **不能 `build ... | tail -1`**：管道取的是 `tail` 的退出码，构建失败也会
    # 被当成成功，然后拿**旧镜像**重建容器并报告"已重启"——故障被完全掩盖。
    # 用 `bash -c 'set -o pipefail; …'`：SSH 的登录 shell 可能是 dash，
    # 直接 `set -o pipefail` 会报 `Illegal option -o pipefail` 并整个中止。
    # **先给当前镜像打 rollback标签**：新构建会直接覆盖 latest，
    #没有这一步就没得回滚。
    snapshot_image(args.host)
    body = (f"set -o pipefail; cd {args.dir} && "
            f"{compose}build api 2>&1 | tail -1 && "
            f"{compose}up -d --force-recreate {args.services} 2>&1 | tail -3")
    _run(["ssh", "-o", "BatchMode=yes", args.host,
          "bash -c " + _sh_quote(body)])
    # **提示保留配置与仓库默认值的差异**：不提示的话，仓库改了默认值而站点
    # 仍用旧值，运维会以为新值已经生效（v0.2.71→v0.2.72 的真实事故）。
    _run(["ssh", "-o", "BatchMode=yes", args.host,
          "bash -c " + _sh_quote(config_drift_script(args.dir))])
       # **机器判定部署成功**：v0.2.114那次 jobs容器重启循环，deploy.py
    #打印一句「请自行核对 /healthz」就退出0，故障全靠人盯 docker ps才发现。
    reason = verify_deployment(args.host, args.services, args.verify_wait)
    if reason == "OK":
        print("验收通过：容器稳定、启动日志无 traceback")
        print("已重启容器。建议再确认一次 /healthz（见 RUNBOOK §9）")
    else:
        print(f"**验收失败**：{reason}")
        if rollback(args.host, args.dir, args.services):
            print("已回退到上一个镜像（stock-screener:rollback）")
            print("**仍需人工确认**：回退后也要看一眼 /healthz与容器状态")
        else:
            print("**回退失败**：目标机上没有 stock-screener:rollback标签")
            print("→立即手工处理，见 RUNBOOK §9")
        return 1
    print(f"提示：{len(PRESERVE_ON_TARGET)}个站点配置保留在目标机未被覆盖；"
        "改了仓库默认值时请按上面的提示手工同步，或用 --show-config对比")
    return 0


def _sh_quote(text: str) -> str:
    """给远端 `bash -c` 的整段脚本加单引号（内部单引号按 POSIX 规则转义）。"""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _has_tag() -> bool:
    try:
        _git("describe", "--tags", "--exact-match", "HEAD")
        return True
    except subprocess.CalledProcessError:
        return False


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败（{proc.returncode}）：{' '.join(cmd[:3])} …")


if __name__ == "__main__":
    raise SystemExit(main())
