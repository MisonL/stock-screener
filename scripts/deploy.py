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
import re
import select
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime
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


def compose_cmd() -> str:
    """`docker compose` 的唯一构造点。**正向部署与回滚共用**。

    `env -u TAG` + 固定 `-p` 两个都不能少：
      · 镜像名是 `stock-screener:${TAG:-latest}`。运维 shell 里若
        `export TAG=v0.2.133`，构建产物与 `_image_id()` 查的不是一个东西 →
        验收假失败 → "回滚"之后 compose 仍按 TAG 拉起**新**镜像，
        等于没回滚；
      · `COMPOSE_PROJECT_NAME` 被改会让容器名与 `verify_script` 里拼的
        `stock-screener-{svc}-1` 对不上，变成假失败。
    Codex 评审指出：只固定正向、回滚仍用原始命令，等于留了半边口子。
    """
    return ("env -u TAG docker compose -p " + PROJECT
            + " -f deploy/docker-compose.yml --project-directory . ")


def rollback(host: str, target: str, services: str) -> tuple[bool, str]:
    """回退到 `stock-screener:rollback`；返回 (是否成功,原因码)。"""
    compose = compose_cmd()
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
    # 取不到输出（ssh 超时 / ssh 不在）＝ **没验成**，不是"坏了"
    if CAPTURE_FAILED in out:
        return "UNKNOWN:拿不到验收输出（" + out.split(CAPTURE_FAILED, 1)[1].strip()[:120] + "）"
    for line in out.splitlines():
        if line.strip() == "SS_VERIFY=OK":
            return "OK"
        if line.startswith("SS_VERIFY=FAIL:"):
            return "FAIL:" + (line[len("SS_VERIFY=FAIL:"):].strip() or "未知原因")
        if line.startswith("SS_VERIFY=UNKNOWN:"):
            return "UNKNOWN:" + (line[len("SS_VERIFY=UNKNOWN:"):].strip() or "未知原因")
    return "UNKNOWN:验收脚本没有给出结论（ssh失败或脚本异常）"


#: `_capture` 拿不到东西时返回这个哨兵。
#:
#: **不能用空串**：`verify_deployment()` 靠"输出里有没有成功标记"判断，
#: 空串会被它当成"跑了但结果为空"，而 ssh 直接卡死/命令不存在是
#: **"根本没验成"**——两者必须分开，前者是 UNKNOWN（退出 3、不回滚），
#: 后者若落到 FAIL 就会去回滚一个健康的新版本。
CAPTURE_FAILED = "\x00SS_CAPTURE_FAILED\x00"


def _capture(cmd: list[str]) -> str:
    """跑命令并取输出（失败也返回已拿到的部分，交给调用方判定）。

    **必须自己吞掉 subprocess 异常**：`timeout=900`，ssh 卡住抛
    `TimeoutExpired`、ssh 不存在抛 `FileNotFoundError`。不捕获的话
    `main()` 会以 traceback + 退出码 1 结束——而 1 的语义是
    「确认坏、已回滚」，值班会去手工回滚一个其实健康的新版本。

    **异常文本里绝不能带命令原文**（Codex 评审抓到的阻断）：
    `TimeoutExpired.__str__` 是 `Command '[...]' timed out`，而 `cmd`
    **包含 bash 脚本全文**。于是 `SS_BACKUP=OK` 这种"成功标记"会以字面量
    出现在异常文本里，备份函数用 `"SS_BACKUP=OK" not in out` 判断时
    **误判成功 → 放行一次不可逆迁移**。只保留异常类型名，不带 `str(exc)`。
    """
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True,
                              timeout=900)
    except (subprocess.SubprocessError, OSError) as exc:
        # **只留类型名**，绝不拼 str(exc)（它会带出整条命令）
        return f"{CAPTURE_FAILED}{type(exc).__name__}"
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


def check_public_health(url: str, timeout_s: int =20, expect_version: str = "") -> str:
    """从**外部**探一次 `/healthz`；返回 "OK" / "FAIL:<原因>" / "SKIP"。

    容器 healthy只说明容器内部自洽。cloudflared挂了、DNS改了、证书过期，
    容器全是 `Up (healthy)`，而用户一个都访问不到——这一层必须单独验。

    给了 expect_version时，还要求响应里就是**这一次部署的版本**：
    `ok:true`只证明"有东西在应答"，CDN缓存、另一个实例、回滚到旧版
    都能满足它。

    url为空时返回 SKIP（站点没配公网地址，或本地调试）。
    """
    if not url:
        return "SKIP"
    try:
        # **必须带 User-Agent**：Cloudflare把默认的 `Python-urllib/x.y`判成爬虫
        #返回403——实测同一地址 `curl`200、`urllib`403、自定义 UA200。
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = resp.status
            body = resp.read(4000).decode("utf-8", "replace")
    except Exception as exc:
        return f"FAIL:请求失败 {type(exc).__name__}: {exc}"
    if code !=200:
        return f"FAIL:HTTP {code}"
    flat = body.replace(" ", "").replace(chr(10), "")
    if chr(34) + "ok" + chr(34) + ":true" not in flat:
        return f"FAIL:响应里没有 ok:true：{body[:120]}"
    if expect_version:
        want = chr(34) + "version" + chr(34) + ":" + chr(34) + expect_version + chr(34)
        if want not in flat:
            return f"FAIL:对外版本不是这次部署的（期望 {expect_version}）：{body[:120]}"
    return "OK"


    

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "core" / "schema"


def strip_sql_comments(sql: str) -> str:
    """去掉 `--` 行注释，避免注释里的 "drop table" 造成误判。"""
    out = []
    for line in sql.splitlines():
        out.append(line.split("--",1)[0])
    return "\n".join(out)


def classify_migration(sql: str) -> str:
    """把一份迁移归类为 `additive` 或 `irreversible`。

    **additive**（加表/加索引/加列）可以安全地被「回滚代码」跨过：
    旧代码不认识新列，它读自己用到的那些照样工作。

    **irreversible** 有两类，都不是「回退代码」能撤销的：

    1. **改结构**（`DROP TABLE` / `RENAME TO`）：表被整张换过，旧代码可能读不了。
       `004_fetch_log_gap.sql` 就是 `CREATE新的 → DROP旧的 → RENAME`。
    2. **改数据**（`UPDATE` / `DELETE`）：表结构没变，但数据被改写了。
       `006_gap_pending_target_day.sql` 就是一条 UPDATE，它修的是旧语义写坏的行——
       回退代码之后，那些行已经被改成新语义了。

    认不出来的语句一律按 `irreversible` 处理——宁可多备份一次。
    """
    body = strip_sql_comments(sql).upper()
    if "DROP TABLE" in body or "RENAME TO" in body:
        return "irreversible"
    for stmt in body.split(";"):
        s = stmt.strip()
        if not s:
            continue
        if s.startswith(("CREATE ", "ALTER TABLE", "PRAGMA", "INSERT ")):
            continue
        return "irreversible"
    return "additive"


def pending_risky_migrations(applied: list[str] | None, schema_dir: Path) -> list[str]:
    """返回「还没应用、且不可逆」的迁移文件名。

    `applied` 为 None（问不到远端状态）时**全部视为待应用**：
    宁可多备份一次，也不要因为读不到就跳过备份。
    """
    have = set(applied or [])
    risky: list[str] = []
    for path in sorted(schema_dir.glob("*.sql"), key=lambda p: p.name):
        if path.name in have:
            continue
        if classify_migration(path.read_text(encoding="utf-8")) == "irreversible":
            risky.append(path.name)
    return risky


def backup_before_upgrade(host: str, target: str) -> str:
    """在目标机上用 sqlite3 的 backup API 复制一份 `.pre-upgrade`。

    **不能用 `cp`**：WAL 模式下未 checkpoint 的数据在 `-wal` 里，
    `cp` 出来的库会缺最近的事务，甚至在被写入时拷出半个页。

    返回 "OK" / "FAIL:<原因>"。
    """
    db = target.rstrip("/") + "/data/market.db"
    # **文件名带时间戳**：原来写死 `.pre-upgrade`，每次不可逆迁移前都覆盖同一份。
    # 首次升级生成的"旧结构"备份，在**下一次**不可逆迁移时会被覆盖成
    # "已含上次新结构"的库——此时想回退到更早那个点，原始备份已经没了。
    # 带时间戳后每份都留着（只保最近 3 份，避免占磁盘）。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = f"{db}.pre-upgrade-{stamp}"
    code = (
     "import sqlite3;"
     "s=sqlite3.connect('file:" + db + "?mode=ro',uri=True);"
     "d=sqlite3.connect('" + dst + "');"
     "s.backup(d);d.close();s.close();"
     "print('SS_BACKUP=OK')"
     )
    # **只保最近 3 份**：一份 1.3GB，攒多了会把本来就紧的磁盘占满。
    # 用 `ls -t | tail -n +4` 删更旧的，不管删除失败（不影响本次备份）。
    prune = (
     "ls -t '" + db + "'.pre-upgrade-* 2>/dev/null | tail -n +4 | xargs -r rm -f; true"
    ) if os.name != "nt" else "true"
    out = _capture(["ssh", "-o", "BatchMode=yes", host,
     "bash -c " + _sh_quote("python3 -c " + _sh_quote(code) + " && (" + prune + ")")
     + " || true"])
    # **哨兵优先判定**：拿不到输出（ssh 超时/不存在）绝不能被当成备份成功——
    # 那会放行一次没有备份的不可逆迁移。
    if CAPTURE_FAILED in out:
        return "FAIL:拿不到备份命令的输出（" + out.split(CAPTURE_FAILED, 1)[1].strip()[:80] + "）"
    # 只认**整行相等**的标记：子串匹配会被"回显了命令原文"的输出骗过
    if "SS_BACKUP=OK" not in {ln.strip() for ln in out.splitlines()}:
        return "FAIL:" + (out.strip()[:200] or "没有输出")
    return "OK"


def read_schema_version(host: str, target: str) -> list[str] | None:
 """读目标机 `data/market.db`里已应用的迁移名；读不到返回 None。

 **只读**打开（`mode=ro`），不建库、不迁移。用 python3 + sqlite3模块，
不依赖宿主机有没有 sqlite3 CLI（容器里就没有）。

 返回 None与返回 [] 必须区分：None是「问不到」，[]是「库是空的」。
 """
 db = target.rstrip("/") + "/data/market.db"
 code = (
 "import sqlite3,sys;"
 "c=sqlite3.connect('file:" + db + "?mode=ro',uri=True);"
 "print('|'.join(r[0] for r in c.execute('select name from schema_version order by name')))"
 )
 out = _capture(["ssh", "-o", "BatchMode=yes", host,
 "python3 -c " + _sh_quote(code) + " || true"])
 for line in out.splitlines():
    if "|" in line or line.strip().startswith("0"):
        return [x for x in line.strip().split("|") if x]
    if line.strip() == "":
        continue
 return None


def schema_drift_warning(before: list[str] | None, after: list[str] | None) -> str:
 """回滚后比较迁移集合，返回告警文本；没有漂移返回空串。

 **回滚只回代码，不回数据**：如果这次部署应用了新迁移，回退代码后
旧代码面对的是**新结构**的库。加列/加表通常无碍，但重建过表
（如 004_fetch_log_gap.sql）就可能让旧代码读不了。
 """
 if before is None or after is None:
    return ""
 new = [x for x in after if x not in before]
 if not new:
    return ""
 return (
 "**警告：数据库结构已经前进，回滚只回代码、不回数据**\n"
 " 本次部署新应用的迁移：" + "、".join(new) + "\n"
 " 旧代码现在面对的是新结构的库。加列/加表通常无碍，"
 "重建过表的（如 004）可能读不了。\n"
 " 处理：先确认 /healthz 与日志；必要时按 RUNBOOK §9.2 用"
 " 最新的 data/market.db.pre-upgrade-<时间戳> 恢复数据库（会丢这次部署后的新数据）。"
 )


PROJECT = "stock-screener"


def prune_old_images(host: str) -> str | None:
    """只删**本项目**的悬空镜像；返回删掉了几个，没跑成返回 `None`。

    每次 `compose build` 都会产生新镜像，上一次的 `rollback`标签被新镜像
    顶掉后就成了悬空镜像。实测一天13次发布留下9个639MB悬空镜像（约5.7GB）。

    **只按 `com.docker.compose.project` 标签筛**：这台机器上还跑着
    WeKnora / selfhost / langbot 等项目，`docker image prune -f` 会把它们的
    悬空镜像一起删掉（实测有一个 nginx 的129MB悬空镜像，不是我们的）。

    **绝不删 `latest` / `rollback`**：它们有标签，本来就不在悬空列表里；
    这里也不再调 `docker image prune -a`——那会把 `rollback` 一起抹掉，
    回滚能力随之消失。

    只在**验收通过之后**调用：部署失败时 `rollback` 是刚需。
    """
    code = (
     "for id in $(docker images -f dangling=true -q); do "
     "p=$(docker image inspect --format '{{index .Config.Labels \"com.docker.compose.project\"}}' $id 2>/dev/null); "
     f"if [ \"$p\" = \"{PROJECT}\" ]; then "
     "docker rmi $id >/dev/null 2>&1 && echo SS_PRUNED=$id; "
     "fi; done; echo SS_DONE"
     )
    out = _capture(["ssh", "-o", "BatchMode=yes", host, "bash -c " + _sh_quote(code)])
    # **必须区分「删了 0 个」和「根本没跑成」**：ssh 连不上、docker 挂了
    # 时 `SS_PRUNED=` 一行也没有，只看计数会把两者混成"0"，
    # 于是清理失败也静默。用结尾哨兵判断是否真的跑完。
    if CAPTURE_FAILED in out:
        return None
    if "SS_DONE" not in out:
        return None
    n = sum(1 for line in out.splitlines() if line.startswith("SS_PRUNED="))
    return str(n)


#: 磁盘越过这个百分比才回收构建缓存。**平时不主动清**：
#: 清掉缓存只是让下次构建变慢，没坏处但也没好处——只在真有压力时动手。
#:
#: **不能定得比稳态水位低**：本机常年 85~90%，定 80 等于每次部署都去清
#: 同机 WeKnora/selfhost/langbot 的缓存（构建缓存无法按项目筛）。
#:
#: **更不能随便清**：清缓存会连带删掉本项目的 pip/npm/vite 层，下次
#: 部署就得完整重跑（`pip install pandas/numpy/TA-Lib` + 两次 `npm ci` +
#: `vite build`）——这是**满核几分钟**的 CPU 尖峰。这台机器的痛点是
#: 负载（曾实测 ssh 卡住、healthz 530），不是磁盘写满（常年还有 10G）。
#: **用 CPU 换磁盘是换错方向**。所以阈值定在 95——真正逼近写满、
#: 不回收就要出事时才动，而不是每次发版都付重建代价。
DISK_RECLAIM_PCT = 95.0


#: **不能部署的时间窗口（本地时间）**。`up -d --force-recreate` 会同时重启
#: api/jobs/agent，而 `jobs` 里跑着几个**长时间、不可中断**的任务。
#:
#: 2026-11-24 的真实事故：23:30:14 部署、23:33:12 新容器起来，
#: 正好吃掉 23:30 的每日备份——`.market-20261124.db.tmp` 残留，
#: **那一晚的备份永久丢失**（直到次日 08:55 才报一条 backup_stale）。
#:
#: 现在 jobs 有了 `stop_grace_period: 180s`（能等 EOD/备份做完），
#: 但备份本身要 8 分钟，超过 180s 仍会被杀。所以在源头挡住：
#: 真要在这个窗口部署，显式加 `--force-window`。
DEPLOY_BLOCK_WINDOWS = (
    # (起, 止, 原因)
    ((15, 0), (15, 40), "EOD 写库（约 3 分钟）+ archive 归档（15:30）"),
    ((23, 25), (23, 50), "每日备份（23:30 起，约 8 分钟，含 integrity_check）"),
    ((8, 35), (9, 10), "开盘前补跑 EOD / 除权刷新 / 自检（08:40~09:00）"),
)


#: 等远端回一个"锁结果"的上限。超时就当拿不到——**不能无限等**：
#: 远端脚本可能在 `flock` 成功之后、`echo` 之前卡住（hung mount、
#: `~/.ssh/rc` 里挂住），此时控制台一行提示都没有，人只会以为"在构建"，
#: 而远端可能**已经持着锁**，把别人也一起挡住。
LOCK_RESULT_TIMEOUT_S = 30

#: 持锁期间向远端发心跳的间隔。远端 `read -t 180`，本地 60 秒续一次：
#: 正常部署不会被误判超时，而本地机器真失联时远端最多 3 分钟就自己
#: 退出并把锁还给内核（否则锁会一直被那条半死的会话占着）。
LOCK_HEARTBEAT_S = 60

#: 远端噪声音符（ANSI 转义）。`~/.ssh/rc`、包装脚本的欢迎语都是写进会话
#: **stdout** 的，会出现在我们的判定行之前。
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean_remote_line(raw: str) -> str:
    """剥掉 ANSI 转义并去空白——远端杂行不能污染"锁结果"的判定。"""
    return _ANSI.sub("", raw).strip()


def _acquire_deploy_lock(host: str, lock_path: str):
    """在目标机上拿**真正的排他锁**；拿到返回持有它的 ssh 子进程，被占返回 `None`。

    ## 为什么不再用「锁文件 + mtime 陈旧判定」

    那个做法有 **TOCTOU**：抢占是「读 mtime → 删 → 重建」三步，两个进程可以
    都判陈旧、都删除、都重建成功，各自以为自己拿到了锁。释放端
    「读内容 → 比 token → 删」同理，慢部署结束时可能删掉**后来者**的锁。

    ## 现在：`flock` 由一条常驻 ssh 会话持有

    部署由几十条独立 ssh 命令组成，所以 `ssh 'flock -n 9'` 一退出锁就没了。
    办法是让**一个** ssh 进程专门持锁：拿到 fd 9 上的 `flock` 之后，远端脚本
    阻塞在 `read` 上不退出；本地一关 stdin，远端读到 EOF、脚本结束、fd 关闭、
    **内核**释放锁。

    ## 这条连接必须被当成"有生命的东西"对待

    锁的正确性现在挂在一条**几乎不发数据**的 TCP 连接上，所以三件事缺一不可：

    1. **保活**（`ServerAliveInterval`）：OpenSSH 默认的 `TCPKeepAlive` 走
       `SO_KEEPALIVE`，Linux 首次探测在**两小时**空闲之后；而 NAT/防火墙的
       空闲超时普遍是 5–30 分钟。首次构建十几分钟 + 验收 90 秒，这条最闲的
       连接最可能被中间设备悄悄丢掉——锁随之释放，而部署**还在继续**，
       于是又变成两个部署并行。保活让它保持活跃，断了也能被及时发现。
    2. **读超时**：见 `LOCK_RESULT_TIMEOUT_S`。
    3. **锁存活校验**：`_deploy_body` 在每个关键边界调 `_lock_lost()`，
       断了就中止，绝不"锁没了还继续往下部署"。

    ## 其它几个刻意的选择

    · `-T`：不分配 pty。我们的 stdin 是数据通道，不是终端。
    · `mkdir -p $(dirname $1)`：**全新目标机上第一次部署**时目录还不存在，
      不建目录 `exec 9>` 会失败（实测 `NOFILE`），而新逻辑是 fail-closed，
      会把首次部署硬挡住。
    · 锁路径走**位置参数**（`bash -c <脚本> bash <锁路径>`），不拼进脚本正文：
      路径里有 `"`/`$`/反引号时，拼进去会被远程 shell 解析。
    · 判定只认**整行等于** `SS_LOCK=GOT`（先剥掉 ANSI 噪声，并跳过之前的杂行）。
      一度用一行式 `try:` 拼脚本（Python 语法错误），traceback 把源码行回显
      出来，而源码里含 `SS_LOCK=BUSY` 字面量，子串匹配就把它当"锁被占"——
      **部署被一个根本不存在的锁拦住了**。
    """
    script = "\n".join([
        "set -u",
        # 目录可能还不存在（首次部署）；失败不致命，交给下面的 exec 报 NOFILE
        'mkdir -p "$(dirname "$1")" 2>/dev/null || true',
        'exec 9>"$1" || { echo SS_LOCK=NOFILE; exit 0; }',
        "flock -n 9 || { echo SS_LOCK=BUSY; exit 0; }",
        "echo SS_LOCK=GOT",
        # **持锁**：读 stdin 直到本地关闭（或在 finally 里 kill）。
        # `-t 180` + 本地心跳=60s：万一本地机器失联（网络分区、休眠）而
        # 服务端迟迟收不到 FIN，远端的 `read` 会一直阻塞、**锁就一直不释放**。
        # 加了超时后远端最多 3 分钟就自己退出、内核释放锁。
        # 正常情况下本地每 60 秒发一个空行续期，所以不会误释放。
        "while IFS= read -r -t 180 _line; do :; done",
        "exit 0",
    ])
    argv = [
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        # 保活：20 秒探一次，连续 3 次没回应（≈60 秒）就判定连接已死
        "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3",
        host,
        "bash -c " + _sh_quote(script) + " bash " + _sh_quote(lock_path),
    ]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"**拒绝部署**：拿不到部署锁（无法启动 ssh：{exc}）。")
        return None

    deadline = time.monotonic() + LOCK_RESULT_TIMEOUT_S
    noise = []
    reason = ""
    # **必须按字节读**（Codex 第三轮指出）：`select` 只保证"有数据可读"，
    # 不保证"有一个完整行"。远端如果只送来半行（被切断、或包装脚本输出到一半），
    # `readline()` 会在 `select` 已经返回之后继续死等换行——**30 秒超时形同虚设**，
    # 而这段代码是拿锁/清锁路径，卡住就等于锁被永久占住。
    buf = b""
    fd = proc.stdout.fileno() if hasattr(proc.stdout, "fileno") else None
    while time.monotonic() < deadline:
        if fd is None:
            break
        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError):
            break
        if not ready:
            if proc.poll() is not None:      # 进程已退出，不会有更多输出
                break
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:                        # EOF
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = _clean_remote_line(raw.decode("utf-8", "replace"))
            if line == "SS_LOCK=GOT":
                if noise:
                    print("提示：远端在锁结果之前输出了杂行（已忽略）："
                          + repr(noise[:3]))
                _start_lock_heartbeat(proc)
                return proc
            if line == "SS_LOCK=BUSY":
                reason = "另一个部署正在进行"
                break
            if line == "SS_LOCK=NOFILE":
                reason = f"锁文件所在目录不可创建/不可写：{lock_path}"
                break
            if line:
                noise.append(line[:120])
        else:
            continue                          # 没有凑齐一行，继续读
        break                                 # 命中了 BUSY/NOFILE 分支

    _shutdown_lock_proc(proc)
    if not reason:
        reason = (f"{LOCK_RESULT_TIMEOUT_S} 秒内没有拿到锁结果"
                  if not noise else f"远端输出无法识别：{noise[:3]}")
    print(f"**拒绝部署**：{reason}。")
    print("  锁由内核持有、**不会残留**；确认没有别的部署在跑后再试。")
    return None


def _start_lock_heartbeat(proc) -> None:
    """每 `LOCK_HEARTBEAT_S` 秒往持锁连接写一个空行。

    远端用的是 `read -t 180`：**没有心跳时，一次超过 3 分钟的部署就会被远端
    自己判超时并释放锁**，而本地还在构建——正好又是"两个部署能并行"。
    心跳同时也让这条几乎不发数据的连接不至于被 NAT 判成空闲。

    写失败（连接已断）就静默退出：`_lock_lost()` 会负责发现并中止。
    """
    import threading

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(LOCK_HEARTBEAT_S):
            try:
                if proc.stdin is None or proc.stdin.closed:
                    return
                proc.stdin.write("\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    threading.Thread(target=beat, name="deploy-lock-heartbeat",
                     daemon=True).start()
    try:
        proc._hb_stop = stop
    except AttributeError:
        pass


def _shutdown_lock_proc(proc) -> None:
    """关掉持锁的 ssh：远端读到 EOF 就退出，锁随之释放。"""
    if proc is None:
        return
    # 先停心跳：否则它可能在 stdin 关闭后继续写，抛错污染日志
    stop = getattr(proc, "_hb_stop", None)
    if stop is not None:
        stop.set()
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _lock_lost(holder) -> bool:
    """持锁的那条 ssh 还活着吗？**死了就等于锁没了。**

    保活能发现死连接，但"发现"要几十秒；在它被发现之前，本地可能已经
    `up -d --force-recreate` 完了。所以每个关键边界都要主动查一次。
    """
    return holder is not None and holder.poll() is not None


def _release_deploy_lock(holder) -> None:
    """释放部署锁。

    **不用判断"是不是自己的锁"**：`flock` 挂在这条 ssh 的文件描述符上，
    它只可能释放自己持有的那把，不存在删掉后来者的可能
    （旧实现靠"内容等于自己的 token 才删"，那一步本身就是竞态）。

    ⚠️ **不要 `rm` 锁文件**：`flock` 锁的是 inode，删掉文件后内核锁还挂在
    那个已 unlink 的 inode 上，下一个部署新建同名文件会立刻拿到锁 → 两个
    部署并行。锁只在持有进程退出时释放，"删文件解锁"等于绕过锁。
    """
    _shutdown_lock_proc(holder)


#: 交易所/运维口径的时区。**不能依赖"跑脚本那台机器的本地时区"**：
#: 这个脚本可能在 UTC 机器（CI、跳板机、容器）上跑，那样 16:40 会被算成
#: 08:40，正好落进盘前窗口——实测在 UTC 的测试容器里就是这么误拦的。
SH_TZ = "Asia/Shanghai"


def _sh_now() -> datetime:
    """上海时区的当前时间（naive，便于和窗口元组比较）。"""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(SH_TZ)).replace(tzinfo=None)


def in_blocked_window(now: datetime) -> str | None:
    """`now`（**上海时间**）是否落在"不该部署"的窗口里；返回原因，否则 `None`。

    调用方请用 `_sh_now()`，不要用 `datetime.now()`。
    """
    hm = (now.hour, now.minute)
    for start, end, reason in DEPLOY_BLOCK_WINDOWS:
        # 支持跨零点（这里暂时没有，但写对边界更安全）
        if start <= end:
            if start <= hm < end:
                return reason
        elif hm >= start or hm < end:
            return reason
    return None


def host_disk_pct(host: str) -> float | None:
    """读目标机 `/` 的占用百分比，**与 `df -P` 同口径**；读不到返回 `None`。

    用 `100*(used)/(used+avail)`：ext4 给 root 保留 5%，把它算进分母
    会让读数偏低 4 个多点（详见 `jobs/cleanup.py::disk_used_pct` 的教训）。

    **读不到必须返回 `None` 而不是 0.0**：0 会被当成"磁盘很空"，
    于是缓存回收被静默跳过，一行日志都没有。这台机器的盘位常年在
    80% 以上，恰恰是最需要它工作的时候。

    解析要求**最后一行**且有 6 个字段、第 5 个以 `%` 结尾：
    `_capture` 是 `stdout + stderr` 拼接，stderr（`Warning: Permanently
    added ...`、`ssh: connect ... refused`）若恰好有 6+ 个含数字的 token
    就会被当成 df 数据。加这些约束让失败宁可误判成"读不到"。
    """
    out = _capture(["ssh", "-o", "BatchMode=yes", host, "df -P / | tail -1"])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None
    parts = lines[-1].split()
    if len(parts) < 6 or not parts[4].endswith("%"):
        return None # df 头行 / stderr 里恰好凑够字段的
    try:
        used = float(parts[2])
        avail = float(parts[3])
    except (TypeError, ValueError):
        return None
    if used + avail <= 0:
        return None
    return min(100.0, max(0.0, 100.0 * used / (used + avail)))


def prune_build_cache(host: str, keep_gb: int = 5) -> str:
    """把构建缓存**压到 `keep_gb` 以内**，返回 `docker` 报的回收量。

    **构建缓存无法按项目筛**（buildx 的缓存记录不带 compose 项目标签），
    所以这条会连带影响同机的 WeKnora / selfhost / langbot——代价只是它们
    下次构建慢一点，不会中断任何正在跑的东西。正因如此，
    **只在磁盘越线时才调**（见 `DISK_RECLAIM_PCT`）。

    **为什么不用 `--filter until=72h`**：`until` 判的是**创建时间**，
    不是最后使用时间。这台机器一天发十几次版，缓存几乎都是新的——
    实测跑下来返回 `Total: 0B`，也就是**磁盘越线时它一个字节都收不回来**，
    白跑一趟。`--keep-storage` 判的是总量，行为可预测：
    跑完之后缓存上限就是 `keep_gb`，且由 docker 自己按最旧的先删。
    """
    code = (
     f"docker builder prune -f --keep-storage {keep_gb}GB 2>&1 | tail -1"
     )
    out = _capture(["ssh", "-o", "BatchMode=yes", host, "bash -c " + _sh_quote(code)])
    return out.strip().splitlines()[-1] if out.strip() else ""


def _post_deploy_cleanup(host: str) -> None:
    """验收通过后的清理：悬空镜像 + （磁盘越线时）构建缓存。

    **失败只打印警告，不影响部署结论**：这两件事都不是部署的一部分，
    但它们都会 ssh 出去，ssh 卡住/超时会抛异常。让清理把一次成功的
    部署变成失败，代价远大于它省下的磁盘。
    """
    # 只删本项目的悬空镜像——同机还有 WeKnora/selfhost/langbot 等项目。
    pruned = prune_old_images(host)
    if pruned is None:
        print("警告：镜像清理没跑成（ssh 或 docker 失败），本次跳过")
    elif pruned != "0":
        print(f"已清理 {pruned} 个本项目的旧镜像（悬空，不影响回滚）")

    # **构建缓存无法按项目筛**，只在磁盘越线时才回收：平时清纯属自找麻烦。
    pct = host_disk_pct(host)
    if pct is None:
        print("警告：读不到目标机磁盘水位，跳过构建缓存回收")
        return
    if pct < DISK_RECLAIM_PCT:
        return
    print(f"目标机磁盘 {pct:.1f}%（>= {DISK_RECLAIM_PCT:.0f}%），回收构建缓存")
    freed = prune_build_cache(host)
    # **只把 `Total:` 那行当回收量**：docker 挂了时最后一行是
    # `Cannot connect to the Docker daemon...`，直接打出来像"回收了这么多"。
    if freed.startswith("Total:"):
        print(f"构建缓存：{freed}")
    elif freed:
        print(f"警告：构建缓存回收失败：{freed[:120]}")


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
    ap.add_argument("--skip-backup", action="store_true",
        help="不可逆迁移前不备份（危险，仅用于已知无数据的空库）")
    ap.add_argument("--no-verify", action="store_true",
        help="跳过部署后验收（仅排障用）")
    ap.add_argument("--public-url", default=os.environ.get("SS_PUBLIC_HEALTHZ", ""),
        help="部署后从外部探一次 /healthz（留空则跳过）")
    ap.add_argument("--force-window", action="store_true",
        help="忽略「危险部署窗口」检查（EOD/备份/盘前补跑期间强制部署）")
    args = ap.parse_args(argv)

    # **`--dir ~/x`必须展开**：`_sh_quote`会把路径包进单引号，
    #`~`不会被远端展开，marker写不进去且静默失败。
    args.dir = os.path.expanduser(args.dir)

    # **危险窗口一律拒绝**（除非显式 --force-window）：`--force-recreate`
    # 会重启 jobs，而 EOD（约 3 分钟）、每日备份（约 8 分钟）正在写库/拷文件时
    # 被杀，代价是数据缺失。2026-11-24 就是这么丢了当晚备份。
    if not args.dry_run:
        reason = in_blocked_window(_sh_now())
        if reason is not None:
            if not args.force_window:
                print(f"**拒绝部署**：现在是「{reason}」窗口，重启 jobs 会打断它。")
                print("  等窗口过去，或确认无任务在跑时加 --force-window。")
                return 1
            print(f"警告：正在「{reason}」窗口内强推部署（--force-window）")

    # **本地预检必须在拿锁之前**（Codex 评审）：`check_clean()` 在脏工作区时
    # 抛 `SystemExit`，如果那时已经拿到远端锁，`try/finally` 还没建立，
    # 锁就一直被那条 ssh 占着——别人谁都不能部署，而真正的错误还只是
    # "工作区脏"这种小事。（换成 `flock` 之后不会"残留 40 分钟"了，
    # 但预检失败还占着锁同样没道理。）
    check_clean(allow_dirty=args.allow_dirty)
    sha = _sha()
    tag = _git("describe", "--tags", "--exact-match", "HEAD").strip() \
        if _has_tag() else "(未打 tag)"
    print(f"部署 HEAD={sha} {tag}")
    #从 tag推出期望版本（v0.2.124 →0.2.124）；没打 tag时为空（跳过版本比对）
    expected_version = tag[1:] if tag.startswith("v") and tag[1:2].isdigit() else ""

    # **部署互斥锁**：两个部署同时在跑会互相污染回滚点。
    # 后发者的「上一次验收通过镜像」可能是前发者尚未验完的镜像，
    # 失败时回滚到已知坏版本的概率随之上升。
    # `jobs/backup.py` 已有 flock 先例，这里用同一手法。
    # `flock -n` 拿不到就立刻失败，不排队——排队会让"我以为在部署 A、
    # 实际部署的是 B"更难排查。
    lock_path = args.dir.rstrip("/") + "/.deploy.lock"
    lock_holder = None
    if not args.dry_run:
        lock_holder = _acquire_deploy_lock(args.host, lock_path)
        if lock_holder is None:
            return 1                       # 原因已在 _acquire_deploy_lock 里打印

    try:
        return _deploy_body(args, expected_version, sha, lock_holder)
    finally:
        if lock_holder:
            _release_deploy_lock(lock_holder)


def _assert_not_in_window(args, when: str) -> bool:
    """在**真正重启 jobs 之前**再确认一次不在危险窗口里。

    入口检查不够（Codex 两轮都点）：23:24 通过检查，构建跑到 23:31，
    `up -d --force-recreate` 就直接把 jobs 重启了，而备份正在拷 1.3GB。
    `stop_grace_period: 180s` 也救不了——备份要 8 分钟。

    这里**直接中止**（返回 True，容器未动、不需要回滚），而不是等待：
    等到窗口过去再继续会让部署卡十几分钟，运维会以为卡死。
    返回 `True` 表示"应该中止"，由调用方决定退出码（**不抛异常**，
    因为 `main()` 里抛 `SystemExit` 会绕过测试、也会绕过退出码语义）。
    """
    if args.dry_run or getattr(args, "force_window", False):
        return False
    reason = in_blocked_window(_sh_now())
    if reason is None:
        return False
    print(f"**中止部署（{when}）**：现在进入「{reason}」窗口，"
          "重启 jobs 会打断它。容器未动，代码已同步但未上线；"
          "等窗口过去后重跑，或加 --force-window。")
    return True


#: 部署只重建这几个服务（`--services` 的默认值要与此一致）。
DEFAULT_SERVICES = ("api", "jobs", "agent")


def composed_services_not_recreated(services: str) -> list[str]:
    """`docker-compose.yml` 里有、但本次**不会被重建**的服务。

    **这是 2026-11-26 踩到的真坑**：给 `cloudflared` 改了 compose
    （加 metrics / 加 protocol），跑 `deploy.py` 显示「验收通过」，
    但 `cloudflared` **根本没被重建**——`--services` 默认只含
    api/jobs/agent。于是 git 里的 compose 与实际运行的容器长期不一致，
    而部署日志里一个字都没提。

    更糟的是手工补重建时我自己漏了 `--project-directory .`，
    `./cf` 解析成了 `deploy/cf`（空目录），容器一下子读不到 `config.yml`
    **整站 1033**。所以提示里必须带完整命令，不能只说"记得重建一下"。
    """
    try:
        text = (Path(__file__).parent.parent / "deploy" / "docker-compose.yml") \
            .read_text(encoding="utf-8")
    except OSError:
        return []
    # 只取 `services:` 段下的**两空格缩进键**。直接全文扫 `^  xxx:$` 会把
    # `logging.options:` 之类的子键也算进来（实测列出过 `options`）。
    m = re.search(r"(?ms)^services:\s*$\n(.*?)(?=^[a-z\w$]|\Z)", text)
    block = m.group(1) if m else ""
    declared = re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):\s*$", block)
    wanted = {x.strip() for x in services.split() if x.strip()}
    return [svc for svc in declared if svc not in wanted]


def _deploy_body(args, expected_version: str = "", sha: str = "",
                 lock_holder=None) -> int:
    """`main()` 的后半段（拿锁之后）。单独成函数是为了用 `try/finally` 保证释放锁。"""
    if not sha:
        sha = _sha()
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
        # **上传/解压失败要如实报告**：此前任何一步失败都会抛 SystemExit，
        #退出码非零（正确），但只有堆栈没有一句人话。此时容器还没动，
        #不存在回滚问题，只需要说清楚「部署停在哪一步」。
        try:
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
        except SystemExit as exc:
            print(f"**上传或解压失败，部署中止（容器未动，无需回滚）**：{exc}")
            return 1

    # **必须先 `unset TAG`**（评审抓到）：`docker-compose.yml` 里镜像名是
    # `stock-screener:${TAG:-latest}`。运维 shell 里若 `export TAG=v0.2.133`，
    # `build api` 产出的就是 `stock-screener:v0.2.133`，而 `_image_id()` 查的是
    # `stock-screener:latest`（旧镜像）→ 验收判 FAIL → `rollback()` 把 rollback
    # tag 打到 latest 再 `up -d`，但 compose 仍按 TAG 拉**新镜像**起容器，
    # 于是"回滚"之后跑的还是新版本，状态完全错乱。
    # `--project-name` 同理固定：`COMPOSE_PROJECT_NAME` 被改会让容器名与
    # `verify_script` 里拼的 `stock-screener-{svc}-1` 对不上，变成假失败。
    compose = compose_cmd()
    # **不能 `build ... | tail -1`**：管道取的是 `tail` 的退出码，构建失败也会
    # 被当成成功，然后拿**旧镜像**重建容器并报告"已重启"——故障被完全掩盖。
    # 用 `bash -c 'set -o pipefail; …'`：SSH 的登录 shell 可能是 dash，
    # 直接 `set -o pipefail` 会报 `Illegal option -o pipefail` 并整个中止。
    # **先给当前镜像打 rollback标签**：新构建会直接覆盖 latest，
    #没有这一步就没得回滚。
    # **记下部署前的库结构**：回滚只回代码不回数据，回退后要比对
    # **不可逆迁移前自动备份**：RUNBOOK只是「要求」运维升级前手工备份，
    #而「记不记得」正是故障点。这里在真正动手前替他做掉。
    schema_before = read_schema_version(args.host, args.dir)
    risky = pending_risky_migrations(schema_before, SCHEMA_DIR)
    if risky:
        print("**本次部署包含不可逆迁移**：" + "、".join(risky))
        print("先做一份 data/market.db.pre-upgrade-<时间戳>（sqlite backup API，不是 cp）")
        bk = backup_before_upgrade(args.host, args.dir)
        if bk == "OK":
            print("备份完成：data/market.db.pre-upgrade-<时间戳>（只保最近 3 份）")
        else:
            print("**备份失败**：" + bk)
            if not args.skip_backup:
                print("**已中止部署**：不可逆迁移前没有备份，先修好备份再上线")
                return 1
            print("--skip-backup已指定，继续（风险自担）")
    snapshot_image(args.host, args.dir)
    # **构建前再查一次窗口**（Codex 两轮都点这条）：入口那次检查可能是在
    # 23:24 通过的，而构建要几分钟——真正 `up -d --force-recreate` 时已在
    # 备份窗口内，jobs 被重启，正在拷的 1.3GB 备份被杀（2026-11-24 事故重演）。
    if _lock_lost(lock_holder):
        print("**中止部署（构建前）**：部署锁已丢失（持锁的 ssh 连接断了）。"
              "可能有另一个部署介入，继续下去会污染回滚点。容器未动。")
        return 1
    if _assert_not_in_window(args, "重建前"):
        return 1
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
            schema_after = read_schema_version(args.host, args.dir)
            warn = schema_drift_warning(schema_before, schema_after)
            if warn:
                print(warn)
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
    # **失败只警告，不能翻掉这次部署**：新版本此时已经上线、且已记入
    # `.last_good_image`。这里抛 SystemExit 就成了「部署成功却以非零码结束」，
    # 而退出码 1 的语义是「确认坏、已回滚」——值班会去回滚一个健康的版本。
    drift = _capture(["ssh", "-o", "BatchMode=yes", args.host,
                      "bash -c " + _sh_quote(config_drift_script(args.dir))])
    if drift and CAPTURE_FAILED not in drift:
        print(drift.rstrip())
    elif CAPTURE_FAILED in drift:
        print(f"（配置漂移提示取不到：{drift.split(CAPTURE_FAILED, 1)[1].strip()[:120]}，"
              "不影响本次部署）")
       # **机器判定部署成功**：v0.2.114那次 jobs容器重启循环，deploy.py
    #打印一句「请自行核对 /healthz」就退出0，故障全靠人盯 docker ps才发现。
    # **取这次构建出来的镜像 ID**：验收时要比对容器跑的是不是它。
    #只查 Names/Status会被「build静默失败、up没重建」骗过去。
    if args.no_verify:
        print("**已跳过验收**（--no-verify）：部署是否成功未经验证")
        return 0
    if _lock_lost(lock_holder):
        print("**验收跳过并中止**：部署锁在重启之后丢失了（持锁的 ssh 断开了）。"
              "容器可能已经是新版本，请人工确认状态后再决定是否重跑。")
        return 1
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
        pub = check_public_health(args.public_url,
            expect_version=expected_version)
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
        # **只在验收通过之后清理**：失败时 rollback标签是刚需。
        # 清理不是部署的一部分——**绝不能反过来否决一次成功的部署**：
        # ssh 卡住会抛 TimeoutExpired、ssh 不存在会抛 FileNotFoundError，
        # 未捕获就是「部署成功却以非零码+traceback 结束」，CI/值班会去
        # 回滚一个健康的新版本。所以整段包住，失败只警告。
        try:
            _post_deploy_cleanup(args.host)
        except Exception as exc:                        # noqa: BLE001
            print(f"警告：清理跳过（{type(exc).__name__}: {exc}），不影响本次部署")
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
            schema_after = read_schema_version(args.host, args.dir)
            warn = schema_drift_warning(schema_before, schema_after)
            if warn:
                print(warn)
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
    skipped = composed_services_not_recreated(args.services)
    if skipped:
        print("注意：本次只重建了 " + args.services + "，下面这些服务"
              "的 compose 改动**没有生效**：" + "、".join(skipped))
        print("  它们是常驻基础设施，重建会短暂中断（cloudflared 断隧道、"
              "ntfy 断推送），所以没有放进默认流程。")
        print("  如果这次确实改了它们的配置，部署完手动重建（**必须带 "
              "--project-directory .**，否则 ./cf 会解析成 deploy/cf）：")
        print("    cd " + args.dir + " && env -u TAG docker compose -p " + PROJECT
              + " --project-directory . -f deploy/docker-compose.yml"
              " up -d --force-recreate " + " ".join(skipped))

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
