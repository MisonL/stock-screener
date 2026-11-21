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
import urllib.request
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


def snapshot_image(host: str, target: str) -> str:
    """构建前确定回滚点，返回 MARKER / LATEST / NONE。

    **优先用 `.last_good_image`里记录的镜像 ID**（上次验收通过的），而不是
    「当前 latest」：若上次验收失败且回滚也失败，latest停在坏镜像上，
    再从 latest快照就把**已知坏版本**变成了回滚点。

    **必须是 `$(cat $M)`**：`$M`是文件路径，直接递给 `docker image inspect`
    会报 `invalid reference format`。这条错犯过两次，别再改回去。
    """
    marker = target.rstrip("/") + "/.last_good_image"
    cmd = (
     "M=" + marker + "; "
     "if [ -f $M ]; then "
     "I=$(cat $M); "
     "docker image inspect \"$I\" >/dev/null 2>&1 && "
     "docker tag \"$I\" stock-screener:rollback && echo SS_SNAPSHOT=MARKER; exit 0; "
     "fi; "
     "if docker image inspect stock-screener:latest >/dev/null 2>&1; then "
     "docker tag stock-screener:latest stock-screener:rollback && "
     "echo SS_SNAPSHOT=LATEST; exit 0; fi; "
     "echo SS_SNAPSHOT=NONE"
    )
    out = _capture(["ssh", "-o", "BatchMode=yes", host, cmd])
    for t in ("SS_SNAPSHOT=MARKER", "SS_SNAPSHOT=LATEST", "SS_SNAPSHOT=NONE"):
        if t in out:
            return t.split("=")[1]
    return "UNKNOWN"


def record_good_image(host: str, target: str, image_id: str) -> bool:
    """验收通过后记下镜像 ID，并**回读确认**；返回是否写成功。"""
    marker = target.rstrip("/") + "/.last_good_image"
    cmd = "printf " + _sh_quote("%s\\n") + " " + _sh_quote(image_id) + " > " + _sh_quote(marker)
    _capture(["ssh", "-o", "BatchMode=yes", host, cmd])
    back = _capture(["ssh", "-o", "BatchMode=yes", host,
        "cat " + _sh_quote(marker) + " || true"])
    return image_id in back


def verify_script(services: str, wait_s: int, settle_s: int =20,
        image_id: str | None = None) -> str:
    """远端验收脚本。只认 `SS_VERIFY=OK`一行。

    **为什么要 settle**：`up -d`返回时容器状态就是 `Up0 seconds`，
    「一看到 running就通过」等于启动后几百毫秒就下结论（实测0.8秒）。

    **为什么处处用 `docker ps -a`**：容器退出后 `docker ps`里查不到，
    `case`不匹配任何分支就会被当成「没有 healthcheck」放过去——
    而那正是最该判失败的情况。
    """
    svc = " ".join(f"stock-screener-{x.strip()}-1" for x in services.split() if x.strip())
    expect = (image_id or "").strip()
    return f'''set -u
SVC="{svc}"
EXPECT="{expect}"
start=$(date +%s)
# **settle不占用 wait预算**：deadline从 settle结束后才开始算，
#否则 settle20 + wait90实际只剩70秒，而 jobs的 start_period是300秒。
deadline=$(( start + {settle_s} + {wait_s} ))

#逐个看**目标服务**的状态。不能用 docker ps（只看 running）：
#容器退出后在那里查不到，case不匹配任何分支就会被当成「没有 healthcheck」放过去，
#而那正是最该判失败的情况。
check_svc() {{
 for c in $SVC; do
 st=$(docker ps -a --format '{{{{.Names}}}}|{{{{.Status}}}}' | grep -E "^$c\\|" | head -1)
 case "$st" in
 "") echo "SS_VERIFY=FAIL:容器不存在 $c"; return 1 ;;
 *Restarting*) echo "SS_VERIFY=FAIL:容器在重启 $st"; return 1 ;;
 *unhealthy*) echo "SS_VERIFY=FAIL:不健康 $st"; return 1 ;;
 *Exited*) echo "SS_VERIFY=FAIL:容器已退出 $st"; return 1 ;;
 esac
 done
 return 0
}}

# ----1. settle：先让启动期的异常有机会暴露 ----
while [ $(( $(date +%s) - start )) -lt {settle_s} ]; do
 check_svc || exit 0
 sleep 2
done

# ----2.等 running ----
ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
 check_svc || exit 0
 ok=1
 for c in $SVC; do
 docker ps --filter "name=$c" --filter status=running \
 --format '{{{{.Names}}}}' | grep -qx "$c" || ok=0
 done
 if [ "$ok" = "1" ]; then break; fi
 sleep 2
done

# ----3.等健康检查（有 healthcheck的必须变 healthy）
while [ "$(date +%s)" -lt "$deadline" ]; do
 check_svc || exit 0
 pending=0
 for c in $SVC; do
 st=$(docker ps -a --format '{{{{.Names}}}}|{{{{.Status}}}}' | grep -E "^$c\\|" | head -1)
 case "$st" in *health:*) pending=1 ;; esac
 done
 if [ "$pending" = "0" ]; then break; fi
 sleep 2
 done

# ----4.最后再确认一次：还在跑、不是 restarting、healthcheck已过 ----
check_svc || exit 0
for c in $SVC; do
 docker ps --filter "name=$c" --filter status=running \
 --format '{{{{.Names}}}}' | grep -qx "$c" \
 || {{ echo "SS_VERIFY=FAIL:容器未运行 $c"; exit 0; }}
 # **这里必须再直接查一次**：上面那个等健康的循环一旦因为 deadline已过
 #而一次都没执行，pending会停在0——等于「没等过就判通过」。
 st=$(docker ps -a --format '{{{{.Names}}}}|{{{{.Status}}}}' | grep -E "^$c\\|" | head -1)
 case "$st" in
 *health:*) echo "SS_VERIFY=FAIL:健康检查未通过 $st"; exit 0 ;;
 esac
done

# ----5.跑的是不是这次构建的镜像 ----
if [ -z "$EXPECT" ]; then
 #取不到镜像 ID就不能吹「跑的是新镜像」——直接判无法确认
 echo "SS_VERIFY=UNKNOWN:拿不到本次构建的镜像ID，无法校验"; exit 0
fi
for c in $SVC; do
 got=$(docker inspect --format '{{{{.Image}}}}' "$c" 2>/dev/null || true)
 if [ "$got" != "$EXPECT" ]; then
 echo "SS_VERIFY=FAIL:$c跑的不是新镜像（期望 $EXPECT，实际 $got）"
 exit 0
 fi
done

# ----6.启动日志扫 traceback ----
for c in $SVC; do
 tb=$(docker logs --since 4m "$c" 2>&1 | grep -c 'Traceback (most recent call last)' || true)
 if [ "${{tb:-0}}" -gt 0 ]; then
 detail=$(docker logs --since 4m "$c" 2>&1 \
 | grep -A2 'Traceback (most recent call last)' | head -3 | tr '\\n' ' ')
 echo "SS_VERIFY=FAIL:$c启动异常 $detail"; exit 0
 fi
done
echo SS_VERIFY=OK'''


def rollback(host: str, target: str, services: str) -> tuple[bool, str]:
    """回退到 `stock-screener:rollback`；返回 (是否成功,原因码)。"""
    compose = "docker compose -f deploy/docker-compose.yml --project-directory . "
    body = (
     "set -u; cd " + target + " || { echo SS_ROLLBACK=BAD_TARGET; exit 0; }; "
     "docker image inspect stock-screener:rollback >/dev/null 2>&1 || "
     "{ echo SS_ROLLBACK=NO_IMAGE; exit 0; }; "
     "docker tag stock-screener:rollback stock-screener:latest || "
     "{ echo SS_ROLLBACK=TAG_FAILED; exit 0; }; "
     + compose
     + "up -d --force-recreate " + services + " >/dev/null 2>&1 || "
     + "{ echo SS_ROLLBACK=UP_FAILED; exit 0; }; "
     + "echo SS_ROLLBACK=OK"
    )
    out = _capture(["ssh", "-o", "BatchMode=yes", host, "bash -c " + _sh_quote(body)])
    for line in out.splitlines():
        if line.startswith("SS_ROLLBACK="):
            code = line.split("=",1)[1].strip()
            return code == "OK", code
    return False, "无输出"


def verify_deployment(host: str, services: str, wait_s: int,
        image_id: str | None = None, settle_s: int =20) -> str:
    """返回 "OK" / "FAIL:<原因>" / "UNKNOWN:<原因>"。

    **UNKNOWN与 FAIL必须分开**：ssh抖动、脚本异常属于「没验成」，不能当成
    「部署坏了」去自动回滚——那是把一次成功的部署强行切回旧版本。
    """
    out = _capture(["ssh", "-o", "BatchMode=yes", host,
        "bash -c " + _sh_quote(verify_script(services, wait_s, settle_s, image_id))])
    for line in out.splitlines():
        if line.strip() == "SS_VERIFY=OK":
            return "OK"
        if line.startswith("SS_VERIFY=FAIL:"):
            return "FAIL:" + (line[len("SS_VERIFY=FAIL:"):].strip() or "未知原因")
        if line.startswith("SS_VERIFY=UNKNOWN:"):
            return "UNKNOWN:" + (line[len("SS_VERIFY=UNKNOWN:"):].strip() or "未知原因")
    return "UNKNOWN:验收脚本没有给出结论（ssh失败或脚本异常）"


def _capture(cmd: list[str]) -> str:
    """跑命令并取输出（失败也返回已拿到的部分，交给调用方判定）。"""
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=900)
    return (proc.stdout or "") + (proc.stderr or "")


def _image_id(host: str) -> str:
    """取 `stock-screener:latest`的镜像 ID；取不到返回空串。

    验收要比对容器跑的是不是这次构建出来的镜像——只查 Names/Status
    会被「build静默失败、up没 recreate」骗过去。
    """
    cmd = "docker image inspect --format '{{.Id}}' stock-screener:latest || true"
    out = _capture(["ssh", "-o", "BatchMode=yes", host, cmd])
    for line in out.splitlines():
        if line.startswith("sha256:"):
            return line.strip()
    return ""


UA = "stock-screener-deploy/1.0"


def check_public_health(url: str, timeout_s: int =20) -> str:
    """从**外部**探一次 `/healthz`；返回 "OK" / "FAIL:<原因>" / "SKIP"。

    容器 healthy只说明容器内部自洽。cloudflared挂了、DNS改了、证书过期，
    容器全是 `Up (healthy)`，而用户一个都访问不到——这一层必须单独验。

    url为空时返回 SKIP（站点没配公网地址，或本地调试）。
    """
    if not url:
        return "SKIP"
    try:
        # **必须带 User-Agent**：Cloudflare把默认的 `Python-urllib/x.y`判成爬虫
        #返回403——实测同一地址 `curl`200、`urllib`403。
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = resp.status
            body = resp.read(4000).decode("utf-8", "replace")
    except Exception as exc:
        return f"FAIL:请求失败 {type(exc).__name__}: {exc}"
    if code !=200:
        return f"FAIL:HTTP {code}"
    if chr(34) + "ok" + chr(34) + ":true" not in body.replace(" ", "").replace(chr(10), ""):
        return f"FAIL:响应里没有 ok:true：{body[:120]}"
    return "OK"



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
    ap.add_argument("--settle", type=int, default=20,
        help="验收前先无条件观察多少秒（让启动期异常暴露）")
    ap.add_argument("--no-verify", action="store_true",
        help="跳过部署后验收（仅排障用）")
    ap.add_argument("--public-url", default=os.environ.get("SS_PUBLIC_HEALTHZ", ""),
        help="部署后从外部探一次 /healthz（留空则跳过）")
    args = ap.parse_args(argv)

    # **`--dir ~/x`必须展开**：`_sh_quote`会把路径包进单引号，
    #`~`不会被远端展开，marker写不进去且静默失败。
    args.dir = os.path.expanduser(args.dir)
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
    snapshot_image(args.host, args.dir)
    body = (f"set -o pipefail; cd {args.dir} && "
            f"{compose}build api 2>&1 | tail -1 && "
            f"{compose}up -d --force-recreate {args.services} 2>&1 | tail -3")
    # **build/up失败也要回滚**（Codex两次评审都点）：原来 `_run`抛 SystemExit
    #直接退出，比验收早一步，回滚只在验收分支里——`up`半途失败会留下
    #「一半旧一半新」且无人回退。
    try:
        _run(["ssh", "-o", "BatchMode=yes", args.host,
         "bash -c " + _sh_quote(body)])
    except SystemExit as exc:
        print(f"**构建或重启失败**：{exc}")
        ok, code = rollback(args.host, args.dir, args.services)
        if ok:
            print("已回退到上一个镜像（stock-screener:rollback）")
            #回滚会把 latest指回旧镜像，所以**重新取一次**镜像 ID；
            #传 None会让 EXPECT为空 →复验直接判 UNKNOWN，等于白验。
            back = verify_deployment(args.host, args.services, args.verify_wait,
            image_id=_image_id(args.host), settle_s=args.settle)
            print("回滚后验收：" + ("通过" if back == "OK" else back))
        else:
            print("**回退失败**： " + code)
        return 1
    # **提示保留配置与仓库默认值的差异**：不提示的话，仓库改了默认值而站点
    # 仍用旧值，运维会以为新值已经生效（v0.2.71→v0.2.72 的真实事故）。
    _run(["ssh", "-o", "BatchMode=yes", args.host,
          "bash -c " + _sh_quote(config_drift_script(args.dir))])
       # **机器判定部署成功**：v0.2.114那次 jobs容器重启循环，deploy.py
    #打印一句「请自行核对 /healthz」就退出0，故障全靠人盯 docker ps才发现。
    # **取这次构建出来的镜像 ID**：验收时要比对容器跑的是不是它。
    #只查 Names/Status会被「build静默失败、up没重建」骗过去。
    if args.no_verify:
        print("**已跳过验收**（--no-verify）：部署是否成功未经验证")
        return 0
    image_id = _image_id(args.host)
    reason = verify_deployment(args.host, args.services, args.verify_wait,
        image_id=image_id, settle_s=args.settle)
    if reason == "OK":
        print("验收通过：容器稳定、健康检查通过、跑的是新镜像、无 traceback")
        # **记住这次验收通过的镜像**，下次当回滚点用（避免坏镜像被快照）
        if image_id:
            if not record_good_image(args.host, args.dir, image_id):
             print("**警告**：没能写下 .last_good_image，"
             "下次回滚点会退回当前 latest")
        # **容器 healthy不等于用户能访问**：cloudflared挂了、DNS改了、
        #证书过期时，容器全是 Up (healthy)，而外部一个都打不开。
        pub = check_public_health(args.public_url)
        if pub == "FAIL" or pub.startswith("FAIL:"):
            print("**外部探活失败**：" + pub)
            # **不回滚**：外部不可达多半是隧道/DNS/证书问题，
            #回退代码解决不了，反而把刚验过的版本换掉。
            print("容器与新镜像都已验过；这次失败在公网可达性。见 RUNBOOK §9")
            return 3
        if pub == "OK":
            print("外部探活通过：" + args.public_url)
        else:
            print("（未配置 --public-url，本次没有验公网可达性）")
        print("已重启容器。建议再确认一次 /healthz（见 RUNBOOK §9）")
    else:
        print(f"**验收未通过**：{reason}")
        if reason.startswith("UNKNOWN"):
            # **「没验成」不是「部署坏了」**：ssh抖动/脚本异常都归这里。
            #自动回滚会把一次成功的部署强行切回旧版本——代价比报警大。
            print("无法确认部署状态（不是确认失败），**不自动回滚**。"
             "**新代码已经上线**（up -d已返回0），只是没验成；"
             "请人工核对 /healthz与容器状态，见 RUNBOOK §9")
            # **独立退出码3**：运维/CI才能区分「部署坏了」与「没验成」，
            #否则 exit1会被当成「没部署」而去回滚一个健康的新版本。
            return 3
        ok, code = rollback(args.host, args.dir, args.services)
        if ok:
            print("已回退到上一个镜像（stock-screener:rollback）")
            # **回滚后也要再验收一次**：回滚本身也可能失败（旧镜像也起不来、
            #数据被新版本改过结构）。只打印一句「请人工确认」等于把验证又甩给人。
            #回滚会把 latest指回旧镜像，所以**重新取一次**镜像 ID；
            #传 None会让 EXPECT为空 →复验直接判 UNKNOWN，等于白验。
            back = verify_deployment(args.host, args.services, args.verify_wait,
             image_id=_image_id(args.host), settle_s=args.settle)
            if back == "OK":
             print("回滚后验收通过：已恢复到上一个可用版本")
            else:
             print("**回滚后仍然异常**：" + back)
             print("→立即手工处理，见 RUNBOOK §9")
        else:
            print("**回退失败**： " + code)
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
