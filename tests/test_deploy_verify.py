"""部署验收与回滚的测试。

v0.2.114的事故：jobs容器重启循环，deploy.py打印一句「请自行核对 /healthz」
就退出0——部署是否成功全靠人看。这里把「重启后自动验收 +失败回滚」钉住。

**每条断言都要能红在语义上**，不能只是「某字符串存在」——评审已经抓到
三次恒真断言（参数写死、比较方向反转、marker读取坏掉，测试都照样绿）。
"""
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy


def _noop_archive(dest):
    """造一个合法的空 tar.gz（main()会 tarfile.open它数文件）。"""
    tarfile.open(dest, "w:gz").close()


def _patch_common(monkeypatch, verify="OK", rollback_ok=True, calls=None):
    """把 main()里所有会碰外部的动作都替身掉。"""
    if calls is None:
        calls = []
    monkeypatch.setattr(deploy, "check_clean", lambda **k: None)
    monkeypatch.setattr(deploy, "_sha", lambda: "deadbeef")
    monkeypatch.setattr(deploy, "_has_tag", lambda: False)
    monkeypatch.setattr(deploy, "build_archive", _noop_archive)
    monkeypatch.setattr(deploy, "snapshot_image",
        lambda h, d: calls.append("snapshot") or "MARKER")
    monkeypatch.setattr(deploy, "_image_id", lambda h: "sha256:new")
    monkeypatch.setattr(deploy, "record_good_image",
        lambda h, d, i: calls.append("record:" + i) or True)
    monkeypatch.setattr(deploy, "_run",
        lambda cmd: calls.append("run:" + " ".join(cmd)))
    #部署前后都会读一次库结构；测试里不能真去 ssh。
    #迁移分类与备份：测试里不能真去读仓库 schema / 真备份。
    #清理旧镜像也会 ssh；默认替身成「没删任何东西」。
    #磁盘百分比也会 ssh；默认替身成「很空闲」，避免误触缓存回收。
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 10.0)
    #构建缓存回收同理：默认不触发（host_disk_pct替身返回10%）。
    monkeypatch.setattr(deploy, "prune_build_cache", lambda h, keep_gb=5: "")
    monkeypatch.setattr(deploy, "prune_old_images", lambda h: "0")
    monkeypatch.setattr(deploy, "pending_risky_migrations",
        lambda applied, d: [])
    monkeypatch.setattr(deploy, "read_schema_version",
        lambda h, d: ["001_batch1.sql"])
    monkeypatch.setattr(deploy, "config_drift_script", lambda d: "true")
    # **部署互斥锁会真去 ssh**：默认替身成"拿到了"，再由专门的用例覆盖忙/闲。
    monkeypatch.setattr(deploy, "_acquire_deploy_lock", lambda h, p: FakeLockProc())
    monkeypatch.setattr(deploy, "_release_deploy_lock", lambda holder: None)
    # 危险窗口检查依赖当前时间：默认放行，由窗口用例单独覆盖。
    monkeypatch.setattr(deploy, "in_blocked_window", lambda now: None)
    monkeypatch.setattr(deploy, "verify_deployment",
        lambda h, s, w, **kw: verify)
    def _rb(h, d, s):
        calls.append("rollback")
        return (rollback_ok, "OK" if rollback_ok else "NO_IMAGE")
    monkeypatch.setattr(deploy, "rollback", _rb)
    return calls


def test_verify_script_waits_for_stability(tmp_path):
    """验收脚本必须合法，且**不能一看到 running就下结论**。

    评审实测过 v0.2.116：`up -d`返回时容器就是 `Up0 seconds`，「三个都 running」
    第一轮就成立 →0.8秒返回 OK，healthcheck和启动期崩溃一个都等不到。
    """
    sh = tmp_path / "v.sh"
    sh.write_text(deploy.verify_script("api jobs agent",90,7,"sha256:x"))
    out = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
    assert out.returncode ==0, f"验收脚本不是合法 bash：{out.stderr}"

    body = sh.read_text()
    #1) settle必须来自**参数**：这里传的是7，脚本里就得是7
    assert "-lt 7" in body, f"settle没有用参数7：{[l for l in body.splitlines() if chr(45)+chr(108)+chr(116) in l][:3]}"
    assert "-lt 20" not in body, "settle被写死成20了（--settle失效）"
    #2)等健康检查
    assert "health:" in body and "unhealthy" in body, "没等 healthcheck"
    #3)镜像比对
    assert "EXPECT" in body and ".Image" in body, "没校验跑的是不是新镜像"
    #4)扫 traceback，整行匹配容器名
    assert "Traceback (most recent call last)" in body
    assert "grep -qx" in body, "容器名不是整行匹配——子串会误匹配"


def test_only_exact_ok_line_counts_as_success(monkeypatch):
    """**只有整行等于 SS_VERIFY=OK才算成功**。

    评审的变异：把 `startswith`改成 `in`，6条测试全绿——因为四个用例里
    没有一个包含 OK子串但不以它开头。这里补上这种用例。
    """
    cases = [
        ("SS_VERIFY=OK", "OK"),
        ("SS_VERIFY=FAIL:容器异常 x", "FAIL:容器异常 x"),
        ("", "UNKNOWN"),
        ("docker: command not found", "UNKNOWN"),
        ("SS_VERIFY=FAIL:上次是 SS_VERIFY=OK", "FAIL"),
        ("prefix SS_VERIFY=OK", "UNKNOWN"),
        ("SS_VERIFY=UNKNOWN:拿不到镜像ID", "UNKNOWN:拿不到镜像ID"),
    ]
    for out, want in cases:
        monkeypatch.setattr(deploy, "_capture", lambda cmd, o=out: o)
        got = deploy.verify_deployment("h", "api jobs agent",1)
        assert got.startswith(want), f"输出 {out!r}期望 {want!r}，实际 {got!r}"


def test_image_mismatch_is_detected_end_to_end(monkeypatch):
    """**喂一个错的镜像 ID必须判 FAIL**。

    评审指出：原来只断言 `EXPECT`/`.Image`出现，把比较的 `!=`反转成 `==`
    照样全绿。改成走**真脚本语义**——直接让远端输出「跑的不是新镜像」。
    """
    fake = "SS_VERIFY=FAIL:stock-screener-api-1跑的不是新镜像（期望 sha256:new，实际 sha256:old）"
    monkeypatch.setattr(deploy, "_capture", lambda cmd: fake)
    got = deploy.verify_deployment("h", "api jobs agent",1, image_id="sha256:new")
    assert got.startswith("FAIL:"), got
    assert "跑的不是新镜像" in got, got


def test_image_id_uses_a_valid_docker_format(monkeypatch):
    """**docker --format只能是两个花括号**。

    写成四层 `{{{{.Id}}}}`会让 docker报非法 format、返回空串，
    于是「跑的是不是新镜像」这段静默失效（这个错真发生过）。
    """
    seen = []

    def cap(cmd):
        seen.append(" ".join(cmd))
        return "sha256:abc123"

    monkeypatch.setattr(deploy, "_capture", cap)
    assert deploy._image_id("h") == "sha256:abc123"
    assert "{{.Id}}" in seen[0], seen[0]
    assert "{{{{" not in seen[0], "用了四层花括号——docker会拒绝"


def test_image_id_returns_empty_when_docker_fails(monkeypatch):
    """取不到镜像 ID时返回空串（调用方据此判 UNKNOWN）。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "Error: No such image")
    assert deploy._image_id("h") == ""


def test_snapshot_reads_the_marker_with_cat(monkeypatch):
    """**marker必须用 `$(cat $M)`读**，不能把文件路径当镜像引用。

    `docker image inspect /path/to/.last_good_image`会报
    `invalid reference format`。这个错犯过两次：marker写进去了、却从没被读出来，
    回滚点始终是「当前 latest」。
    """
    seen = []

    def cap(cmd):
        seen.append(" ".join(cmd))
        return "SS_SNAPSHOT=MARKER"

    monkeypatch.setattr(deploy, "_capture", cap)
    mode = deploy.snapshot_image("h", "/srv/app")

    assert mode == "MARKER"
    cmd = seen[0]
    assert "$(cat $M)" in cmd, f"marker不是用 cat读的：{cmd}"
    assert ".last_good_image" in cmd
    assert cmd.index(".last_good_image") < cmd.index("stock-screener:latest"), (
         "先用了 latest——坏镜像会被快照成回滚点")


def test_record_good_image_verifies_the_write(monkeypatch):
    """写完要**回读确认**，写不进去必须返回 False。"""
    calls = []

    def cap(cmd):
        calls.append(cmd)
        return "sha256:abc" if "cat" in " ".join(cmd) else ""

    monkeypatch.setattr(deploy, "_capture", cap)
    assert deploy.record_good_image("h", "/srv/app", "sha256:abc") is True

    calls.clear()
    monkeypatch.setattr(deploy, "_capture", lambda cmd: (calls.append(cmd), "")[1])
    assert deploy.record_good_image("h", "/srv/app", "sha256:abc") is False, \
        "没回读到内容却报告成功"


def test_rollback_reports_the_real_reason(monkeypatch):
    """回滚失败要说清楚是哪一步——不能一律"没有 rollback标签"。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "SS_ROLLBACK=NO_IMAGE")
    ok, code = deploy.rollback("h", "/srv/app", "api jobs agent")
    assert ok is False and code == "NO_IMAGE"


def test_main_rolls_back_when_verification_fails(monkeypatch):
    """验收确认失败 →回滚，且快照发生在构建之前。"""
    calls = _patch_common(monkeypatch, verify="FAIL:jobs启动异常", rollback_ok=True)
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert code !=0, "验收失败却返回成功"
    assert "rollback" in calls, "验收失败但没有回滚"
    snap_i = calls.index("snapshot")
    build_i = next(i for i, c in enumerate(calls) if "build api" in c)
    assert snap_i < build_i, "快照必须在构建之前——否则没得回滚"


def test_unknown_verdict_does_not_roll_back(monkeypatch, capsys):
    """**「没验成」不能当成「部署坏了」去回滚**。

    ssh抖动、脚本异常都会返回 UNKNOWN。自动回滚会把一次成功的部署
    强行切回旧版本并 recreate生产容器——代价远大于报警。

    退出码用3而不是1：否则 CI/运维会把「没验成」当成「没部署」去手工回滚。
    """
    calls = _patch_common(monkeypatch,
        verify="UNKNOWN:验收脚本没有给出结论（ssh失败或脚本异常）")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code == 3, f"UNKNOWN应该用退出码3，实际{code}"
    assert "rollback" not in calls, "「没验成」触发了回滚——会把好部署切回旧版"
    assert "不自动回滚" in out
    assert "新代码已经上线" in out, "没说明代码其实已经上线了"


def test_success_records_the_good_image(monkeypatch, capsys):
    """验收通过才记录"好镜像"，供下次当回滚点。"""
    calls = _patch_common(monkeypatch, verify="OK")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert code ==0
    assert "record:sha256:new" in calls, f"没记录好镜像：{calls}"
    assert "rollback" not in calls


def test_no_verify_flag_skips_verification(monkeypatch, capsys):
    """`--no-verify`必须真的存在（RUNBOOK写过它，曾一度被删）。"""
    calls = _patch_common(monkeypatch, verify="FAIL:不该被调用")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--no-verify"])
    out = capsys.readouterr().out
    assert code ==0, "跳过验收时不该失败"
    assert "已跳过验收" in out
    assert "rollback" not in calls


def test_build_failure_also_rolls_back(monkeypatch, capsys):
    """**build/up失败也要回滚**（Codex两次评审都点了这条）。

    原来 `_run`抛 SystemExit直接退出，比验收早一步，回滚只在验收分支里。
    `up -d`半途失败会留下「一半旧一半新」的栈，且无人回退。
    """
    calls = []

    verifies = []
    def boom(cmd):
        joined = " ".join(cmd)
        if "build api" in joined:
            raise SystemExit("命令失败（1）：ssh … build api")
        calls.append(joined)

    def fake_rollback(h, d, svc):
        calls.append("rollback")
        return (True, "OK")

    monkeypatch.setattr(deploy, "check_clean", lambda **k: None)
    monkeypatch.setattr(deploy, "_sha", lambda: "deadbeef")
    monkeypatch.setattr(deploy, "_has_tag", lambda: False)
    monkeypatch.setattr(deploy, "build_archive", _noop_archive)
    # ⚠️⚠️ 危险窗口必须桩掉（2026-11-26）：本用例断言的是"build 失败也要
    # 回滚"，与**跑测试的时刻**无关。原来漏了这一条，于是 CI 在
    # `(8,35)~(9,10)` 那 35 分钟里必红 —— `[实测]` 08:35:47Z 那轮正是
    # 撞上窗口起点，部署被拒、`_run` 一次都没调用，报的却是
    # 「构建失败但没有回滚」。一天红 35 分钟的用例，等于没有这条门禁。
    # 走 `_patch_common` 的用例都带着这条桩，这条手写的漏了。
    monkeypatch.setattr(deploy, "in_blocked_window", lambda now: None)
    # **部署锁也必须是 fail-closed 的**：换成 flock 之后拿不到锁就返回 None，
    # `main()` 会直接拒绝部署（不再"空 token 继续跑"）。本机跑测试时 ssh 不存在
    # 或连不上，所以这里必须桩掉，否则用例验的是"锁拒绝了部署"而不是"回滚"。
    monkeypatch.setattr(deploy, "_acquire_deploy_lock", lambda h, p: FakeLockProc())
    monkeypatch.setattr(deploy, "snapshot_image", lambda h, d: "MARKER")
    monkeypatch.setattr(deploy, "_run", boom)
    monkeypatch.setattr(deploy, "config_drift_script", lambda d: "true")
    monkeypatch.setattr(deploy, "_image_id", lambda h: "sha256:old")
    monkeypatch.setattr(deploy, "pending_risky_migrations", lambda a, d: [])
    monkeypatch.setattr(deploy, "read_schema_version", lambda h, d: [])
    monkeypatch.setattr(deploy, "rollback", fake_rollback)
    def verify_after_rollback(*args, **kwargs):
        verifies.append(1)
        assert "rollback" in calls, "验收跑在回滚之前了"
        return "OK"
    monkeypatch.setattr(deploy, "verify_deployment", verify_after_rollback)

    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0, "构建失败却返回成功"
    assert "rollback" in calls, "构建失败但没有回滚——会留下半新半旧的栈"
    assert "构建或重启失败" in out
    assert verifies, "回滚后没有复验——把那行删掉测试必须红"

FAKE_DOCKER = """#!/bin/bash
#假 docker：只实现验收脚本用到的几个子命令。
#环境变量 FAKE_IMAGE=容器跑的镜像 ID；FAKE_STATUS=容器状态；FAKE_LOGS=日志内容
case "$1" in
ps)
 case "$*" in
 *status=running*)
 echo "stock-screener-api-1"
 echo "stock-screener-jobs-1"
 echo "stock-screener-agent-1"
 ;;
 *"-a"*)
 for c in stock-screener-api-1 stock-screener-jobs-1 stock-screener-agent-1; do
 echo "$c|${FAKE_STATUS:-Up1 minute (healthy)}"
 done
 ;;
 esac
 ;;
inspect)
 echo "${FAKE_IMAGE:-sha256:new}"
 ;;
logs)
 echo "${FAKE_LOGS:-}"
 ;;
esac
exit 0
"""


def _run_verify(tmp_path, expect, image, status="Up1 minute (healthy)", logs=""):
 """把真脚本跑起来，返回 SS_VERIFY行。"""
 import os
 import subprocess
 bin_dir = tmp_path / "bin"
 bin_dir.mkdir(exist_ok=True)
 docker = bin_dir / "docker"
 docker.write_text(FAKE_DOCKER)
 docker.chmod(0o755)
 script = tmp_path / "v.sh"
 script.write_text(deploy.verify_script("api jobs agent",5,0, expect))
 env = dict(os.environ)
 env["PATH"] = str(bin_dir) + ":" + env["PATH"]
 env["FAKE_IMAGE"] = image
 env["FAKE_STATUS"] = status
 env["FAKE_LOGS"] = logs
 out = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
 return (out.stdout + out.stderr).strip()


def test_real_script_rejects_a_container_on_the_wrong_image(tmp_path):
 """**真脚本**：容器跑旧镜像 →必须 FAIL。

把 `!=`反转成 `==`这条变异，只有真跑脚本才会红。
 """
 got = _run_verify(tmp_path, "sha256:new", "sha256:old")
 assert "SS_VERIFY=FAIL" in got, got
 assert "跑的不是新镜像" in got, got


def test_real_script_accepts_the_right_image(tmp_path):
 """**真脚本**：镜像对、状态健康、无 traceback →OK。"""
 got = _run_verify(tmp_path, "sha256:new", "sha256:new")
 assert got.endswith("SS_VERIFY=OK"), got


def test_real_script_fails_when_there_is_no_expect_id(tmp_path):
 """取不到镜像 ID时必须 UNKNOWN，不能吹"跑的是新镜像"。"""
 got = _run_verify(tmp_path, "", "sha256:new")
 assert "SS_VERIFY=UNKNOWN" in got, got


def test_real_script_fails_on_traceback(tmp_path):
 """启动日志里有 traceback →必须 FAIL。"""
 got = _run_verify(tmp_path, "sha256:new", "sha256:new",
 logs="Traceback (most recent call last): boom")
 assert "SS_VERIFY=FAIL" in got, got
 assert "启动异常" in got, got


def test_real_script_fails_when_container_exited(tmp_path):
 """容器 Exited →必须 FAIL（老实现会把它当成"没有 healthcheck"放过去）。"""
 got = _run_verify(tmp_path, "sha256:new", "sha256:new",
 status="Exited (1)5 seconds ago")
 assert "SS_VERIFY=FAIL" in got, got
 assert "已退出" in got, got


def test_real_script_fails_when_container_restarting(tmp_path):
 """容器在重启循环 →必须 FAIL。"""
 got = _run_verify(tmp_path, "sha256:new", "sha256:new",
 status="Restarting (1)2 seconds ago")
 assert "SS_VERIFY=FAIL" in got, got
 assert "在重启" in got, got


def test_real_script_fails_when_unhealthy(tmp_path):
 """healthcheck报 unhealthy →必须 FAIL。"""
 got = _run_verify(tmp_path, "sha256:new", "sha256:new",
 status="Up2 minutes (unhealthy)")
 assert "SS_VERIFY=FAIL" in got, got
 assert "不健康" in got, got
    

def test_real_script_fails_while_health_is_still_starting(tmp_path):
    """**`health: starting`不能判通过**。

    第三轮评审复现：如果等健康的循环因为 deadline已过而一次都没执行，
    `pending`会停在0，等于「没等过就判通过」。现在结尾会**直接再查一次**状态。
    """
    got = _run_verify(tmp_path, "sha256:new", "sha256:new",
    status="Up3 seconds (health: starting)")
    assert "SS_VERIFY=FAIL" in got, got
    assert "健康检查未通过" in got, got
    

def test_check_public_health_skips_when_url_is_empty():
 """没配公网地址时返回 SKIP（本地调试/无隧道的部署）。"""
 assert deploy.check_public_health("") == "SKIP"


def test_check_public_health_accepts_ok_true(monkeypatch):
 """外部 /healthz返回 ok:true →OK。"""
 class Resp:
    status =200
    def read(self, n):
        return b'{"ok":true,"version":"1.2.3"}'
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
 monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda url, timeout: Resp())
 assert deploy.check_public_health("https://x/healthz") == "OK"


def test_check_public_health_rejects_non_200(monkeypatch):
 """502（隧道挂了）必须 FAIL——这正是容器 healthy却访问不到的场景。"""
 class Resp:
    status =502
    def read(self, n):
        return b"Bad Gateway"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
 monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda url, timeout: Resp())
 assert deploy.check_public_health("https://x/healthz").startswith("FAIL:")


def test_check_public_health_rejects_non_ok_body(monkeypatch):
 """200但内容不是 ok:true（比如被登录页拦住）也不能算通过。"""
 class Resp:
    status =200
    def read(self, n):
        return "<html>login</html>".encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
 monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda url, timeout: Resp())
 assert deploy.check_public_health("https://x/healthz").startswith("FAIL:")


def test_check_public_health_reports_connection_errors(monkeypatch):
 """连不上（DNS/证书/超时）要给出原因，不能抛出去。"""
 def boom(url, timeout):
    raise OSError("Name or service not known")
 monkeypatch.setattr(deploy.urllib.request, "urlopen", boom)
 got = deploy.check_public_health("https://x/healthz")
 assert got.startswith("FAIL:")
 assert "Name or service not known" in got


def test_main_exits_3_without_rollback_when_public_check_fails(monkeypatch, capsys):
 """公网探活失败 →退出码3、**不回滚**。

外部不可达多半是 cloudflared/DNS/证书；回退代码解决不了，
反而会把刚验过的版本换掉。
 """
 calls = _patch_common(monkeypatch, verify="OK")
 monkeypatch.setattr(deploy, "check_public_health",
 lambda url, timeout_s=20, expect_version="": "FAIL:HTTP502")
 code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0",
 "--public-url", "https://x/healthz"])
 out = capsys.readouterr().out
 assert code ==3, f"外部探活失败应该退出3，实际{code}"
 assert "外部探活失败" in out
 assert "rollback" not in calls, "外部故障不该触发代码回滚"
 assert "record:sha256:new" in calls, "镜像本身已验过，应该记为可用"

def test_check_public_health_sends_a_real_user_agent(monkeypatch):
    """**必须带非 urllib的 UA**：Cloudflare把 `Python-urllib/x.y`判成爬虫返回403。

    实测：同一地址 curl200、Python-urllib403、stock-screener-deploy/1.0200。
    丢了 UA这条，门禁会在生产上每次都报"外部探活失败"，而服务其实好得很。
    """
    seen = {}

    class Resp:
        status =200
        def read(self, n):
            return b'{"ok":true}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        return Resp()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fake)
    assert deploy.check_public_health("https://x/healthz") == "OK"
    ua = seen.get("ua") or ""
    assert ua, "没有带 User-Agent"
    assert "urllib" not in ua.lower(), f"UA是 urllib，会被 Cloudflare403：{ua}"

def _stub_urlopen(monkeypatch, body, status=200):
    class Resp:
        pass
    class R(Resp):
        def __init__(self):
            self.status = status
        def read(self, n):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(deploy.urllib.request, "urlopen", lambda req, timeout: R())


def test_public_health_accepts_the_deployed_version(monkeypatch):
    """对外返回的 version与这次部署的一致 →OK。"""
    _stub_urlopen(monkeypatch, b'{"ok":true,"version":"1.2.3"}')
    assert deploy.check_public_health("https://x/healthz", expect_version="1.2.3") == "OK"


def test_public_health_rejects_a_stale_version(monkeypatch):
    """**对外还是旧版本必须 FAIL**。

    `ok:true`只证明"有东西在应答"：CDN缓存、另一个实例、回滚到旧版
    都满足它。只有版本对上，才说明这次部署真的对外生效了。
    """
    _stub_urlopen(monkeypatch, b'{"ok":true,"version":"1.2.2"}')
    got = deploy.check_public_health("https://x/healthz", expect_version="1.2.3")
    assert got.startswith("FAIL:"), got
    assert "对外版本不是这次部署的" in got, got


def test_read_schema_version_parses_the_remote_output(monkeypatch):
    """远端输出的 `a|b|c` 要解析成列表。"""
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: "001_batch1.sql|002_batch2.sql\n")
    got = deploy.read_schema_version("h", "/srv/app")
    assert got == ["001_batch1.sql", "002_batch2.sql"], got


def test_read_schema_version_returns_none_when_it_cannot_tell(monkeypatch):
    """**问不到要返回 None，不能返回 []**：[]是"库是空的"，
    None是"没问到"。回滚告警依赖这个区别，混了就会漏报。"""
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: "ssh: connect to host: Connection refused\n")
    assert deploy.read_schema_version("h", "/srv/app") is None


def test_schema_drift_warning_is_silent_without_drift():
    """没应用新迁移就不该刷屏。"""
    v = ["001_batch1.sql", "002_batch2.sql"]
    assert deploy.schema_drift_warning(v, v) == ""


def test_schema_drift_warning_is_silent_when_unreadable():
    """读不到（None）时不能瞎告警，也不能假装没问题。"""
    assert deploy.schema_drift_warning(None, ["001_batch1.sql"]) == ""
    assert deploy.schema_drift_warning(["001_batch1.sql"], None) == ""


def test_schema_drift_warning_names_the_new_migrations():
    """告警里必须点出**具体是哪几个迁移**，运维才知道去查什么。"""
    warn = deploy.schema_drift_warning(
     ["001_batch1.sql"], ["001_batch1.sql", "009_new.sql"])
    assert "009_new.sql" in warn
    assert "只回代码" in warn
    assert "RUNBOOK" in warn




def test_rollback_after_a_new_migration_warns_about_schema_drift(monkeypatch, capsys):
    """**回滚只回代码不回数据**：这次部署应用了新迁移时，
    回退后必须告警——旧代码面对新结构可能读不了。
    """
    calls = _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    seq = iter([["001_batch1.sql"], ["001_batch1.sql", "009_new.sql"]])
    monkeypatch.setattr(deploy, "read_schema_version",
        lambda h, d: next(seq))
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0
    assert "数据库结构已经前进" in out, f"没告警 schema漂移：{out[-400:]}"
    assert "009_new.sql" in out


def test_rollback_without_a_new_migration_does_not_warn(monkeypatch, capsys):
    """没应用新迁移时不该刷无意义的告警。"""
    calls = _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    monkeypatch.setattr(deploy, "read_schema_version",
     lambda h, d: ["001_batch1.sql"])
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0
    assert "数据库结构已经前进" not in out


def test_classify_migration_treats_add_column_as_additive():
    """加列是 additive：旧代码不认识新列，读自己用的那些照样工作。"""
    assert deploy.classify_migration(
     "ALTER TABLE agent_schedule ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;"
     ) == "additive"


def test_classify_migration_treats_table_rebuild_as_irreversible():
    """`CREATE新的 → DROP旧的 → RENAME` 换掉了整张表，旧代码可能读不了。"""
    sql = ("CREATE TABLE fetch_log_new(id INTEGER);"
     "DROP TABLE fetch_log;"
     "ALTER TABLE fetch_log_new RENAME TO fetch_log;")
    assert deploy.classify_migration(sql) == "irreversible"


def test_classify_migration_treats_data_update_as_irreversible():
    """**改数据也算不可逆**：006是一条 UPDATE，回退代码撤销不了它。

    这是最容易漏的一类——表结构没动，看起来「安全」，
    但旧代码回来面对的是被改写成新语义的行。
    """
    assert deploy.classify_migration(
     "UPDATE gap_pending SET missing_through = 1 WHERE missing_through IS NOT NULL;"
     ) == "irreversible"


def test_classify_migration_ignores_drop_in_comments():
    """注释里出现 "DROP TABLE" 不能误判。"""
    sql = "-- 这里说明为什么要 DROP TABLE 旧表\nCREATE TABLE t(a INTEGER);"
    assert deploy.classify_migration(sql) == "additive"


def test_pending_risky_migrations_only_returns_unapplied_ones(tmp_path):
    """已应用的不可逆迁移不该再触发备份。"""
    (tmp_path / "001_a.sql").write_text("CREATE TABLE t(a INTEGER);")
    (tmp_path / "002_b.sql").write_text("DROP TABLE t;")
    assert deploy.pending_risky_migrations(["001_a.sql", "002_b.sql"], tmp_path) == []
    assert deploy.pending_risky_migrations(["001_a.sql"], tmp_path) == ["002_b.sql"]


def test_pending_risky_migrations_treats_unknown_state_as_all_pending(tmp_path):
    """读不到远端状态（None）时**全部视为待应用**：
    宁可多备份一次，也不要因为读不到就跳过备份。"""
    (tmp_path / "002_b.sql").write_text("DROP TABLE t;")
    assert deploy.pending_risky_migrations(None, tmp_path) == ["002_b.sql"]


def test_backup_before_upgrade_reports_failure(monkeypatch):
    """备份失败要如实返回，不能假装成功。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "sqlite3.OperationalError: disk I/O error")
    assert deploy.backup_before_upgrade("h", "/srv/app").startswith("FAIL:")


def test_backup_before_upgrade_uses_the_backup_api_not_cp(monkeypatch):
    """**必须用 sqlite3.backup()，不能用 cp**：
    WAL 下未 checkpoint 的数据在 -wal 里，cp 出来的库会缺事务。"""
    seen = []
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: (seen.append(" ".join(cmd)), "SS_BACKUP=OK")[1])
    assert deploy.backup_before_upgrade("h", "/srv/app") == "OK"
    assert "backup(" in seen[0], seen[0]
    assert "cp " not in seen[0], "用了 cp"


def test_irreversible_migration_triggers_a_backup_before_deploy(monkeypatch, capsys):
    """待应用不可逆迁移 →部署前先备份。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "pending_risky_migrations",
     lambda applied, d: ["009_drop.sql"])
    monkeypatch.setattr(deploy, "backup_before_upgrade",
     lambda h, d: (calls.append("backup"), "OK")[1])
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code ==0
    assert "backup" in calls, "不可逆迁移前没备份"
    build_i = next(i for i, c in enumerate(calls) if "build api" in c)
    assert calls.index("backup") < build_i, "备份必须在构建之前"


def test_backup_failure_aborts_the_deploy(monkeypatch, capsys):
    """备份失败 →**中止部署**。没备份就上线不可逆迁移是赌博。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "pending_risky_migrations",
     lambda applied, d: ["009_drop.sql"])
    monkeypatch.setattr(deploy, "backup_before_upgrade",
     lambda h, d: "FAIL:disk I/O error")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0
    assert "已中止部署" in out
    assert not any("build api" in c for c in calls), "备份失败却还是构建了"


def test_scp_failure_stops_the_deploy_before_any_build(monkeypatch, capsys):
    """**scp失败要给人话，且绝不能继续构建/重启**。"""
    calls = _patch_common(monkeypatch, verify="OK")
    def boom(cmd):
        joined = " ".join(cmd)
        if "scp" in joined:
            raise SystemExit("命令失败（1）：scp …")
        calls.append(joined)
    monkeypatch.setattr(deploy, "_run", boom)
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0
    assert "上传或解压失败" in out, f"没有一句人话：{out[-300:]}"
    assert "容器未动" in out
    assert not any("build api" in c for c in calls), "scp失败却还构建了"

def test_extract_failure_also_stops(monkeypatch, capsys):
    """解压失败同样中止——半套新代码的目录比没有更危险。"""
    calls = _patch_common(monkeypatch, verify="OK")
    def only_extract(cmd):
        joined = " ".join(cmd)
        if "src-deadbeef" in joined and "tar -xzf" in joined:
            raise SystemExit("命令失败（2）：ssh … tar -xzf")
        calls.append(joined)
    monkeypatch.setattr(deploy, "_run", only_extract)
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0
    assert "上传或解压失败" in out


def _fake_docker_env(tmp_path, monkeypatch, images):
    """把 `prune_old_images` 真正跑一遍：造一个假 `docker` 丢进 PATH。

    **为什么不能只断言命令字符串**：上一版测试是"我在 fake `_capture` 里写
    几行 `SS_PRUNED=`，函数就数出几"，对**过滤逻辑零覆盖**——把
    `[ "$p" = "stock-screener" ]` 改成 `!=`（会删光别人的镜像）它照样绿，
    把标签模板写成四层花括号（历史上真发生过，删 0 个）它也照样绿。

    images: [(id, label 或 None)]，None 表示该镜像**没有** compose 项目标签。
    返回被 `rmi` 掉的 ID 列表（读文件，不靠 print）。
    """
    import os
    import shlex
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    labels = tmp_path / "labels.txt"
    labels.write_text("\n".join(f"{i}\t{l or ''}" for i, l in images), encoding="utf-8")
    removed = tmp_path / "removed.txt"
    removed.write_text("", encoding="utf-8")

    # 假 docker：用 python 写，避免多层引号（bash+awk+f-string）互相打架。
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"labels = pathlib.Path({str(labels)!r})\n"
        f"removed = pathlib.Path({str(removed)!r})\n"
        "m = {}\n"
        "for line in labels.read_text().splitlines():\n"
        "    i, _, lab = line.partition(chr(9))\n"
        "    m[i] = lab\n"
        "if sys.argv[1] == 'images':\n"
        "    print(chr(10).join(m))\n"
        "elif sys.argv[1] == 'image' and sys.argv[2] == 'inspect':\n"
        "    print(m.get(sys.argv[-1], ''))\n"
        "elif sys.argv[1] == 'rmi':\n"
        "    with removed.open('a') as fh:\n"
        "        fh.write(sys.argv[2] + chr(10))\n",
        encoding="utf-8")
    fake.chmod(0o755)

    def fake_capture(cmd):
        # cmd = ["ssh","-o","BatchMode=yes","h","bash -c <code>"]
        code = shlex.split(cmd[-1])[-1]
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        p = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                           env=env, timeout=30)
        return (p.stdout or "") + (p.stderr or "")

    monkeypatch.setattr(deploy, "_capture", fake_capture)
    return removed


def test_prune_only_targets_dangling_images_of_this_project(tmp_path, monkeypatch):
    """**只删本项目的悬空镜像**：真跑一遍假 `docker`，看它 rmi 了谁。

    这台机器上还跑着 WeKnora / selfhost / langbot，`docker image prune -f`
    会把它们的悬空镜像一起删掉（实测有一个 129MB 的 nginx 悬空镜像不是我们的）。
    """
    images = [
        ("aaa111", "stock-screener"),
        ("bbb222", "weknora-app"),# 别人的悬空镜像，不能动
        ("ccc333", None),# 没有 compose 项目标签，不能动
        ("ddd444", "stock-screener"),
    ]
    removed = _fake_docker_env(tmp_path, monkeypatch, images)

    assert deploy.prune_old_images("h") == "2"
    已删 = removed.read_text(encoding="utf-8").split()
    assert sorted(已删) == ["aaa111", "ddd444"], f"删错了：{已删}"


def test_prune_reports_failure_instead_of_zero(tmp_path, monkeypatch):
    """**「删了 0 个」和「根本没跑成」必须分得开。**

    ssh 连不上 / docker 挂了时一行 `SS_PRUNED=` 都没有，只看计数会把
    两者混成 "0"，于是清理失败也静默——这台机器的盘位常年在 85% 以上。
    """
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: "ssh: connect to host h port 22: Connection refused\n")
    assert deploy.prune_old_images("h") is None


def test_prune_never_uses_prune_a_or_touches_the_rollback_tag(monkeypatch):
    """**绝不能出现 `image prune -a`**：那会删掉 `stock-screener:rollback`，
        回滚能力随之消失。也不能显式 rmi 那个标签。"""
    seen = []
    monkeypatch.setattr(deploy, "_capture",
        lambda cmd: (seen.append(" ".join(cmd)), "")[1])
    deploy.prune_old_images("h")
    cmd = seen[0]
    assert "prune -a" not in cmd and "prune --all" not in cmd, "用了 -a，会删掉回滚镜像"
    assert "stock-screener:rollback" not in cmd, "动了回滚标签"
def test_prune_returns_zero_when_nothing_matches(monkeypatch):
    """没有可删的就返回 "0"，调用方据此不打印。

    但**跑完了却没删到**和**根本没跑成**必须分开：
    哨兵 `SS_DONE` 在就说明命令跑完了，返回 "0"；不在就返回 None。
    """
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "SS_DONE\n")
    assert deploy.prune_old_images("h") == "0"
def test_successful_deploy_prunes_old_images(monkeypatch, capsys):
    """验收通过之后才清理。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "prune_old_images",
        lambda h: (calls.append("prune"), "3")[1])
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code ==0
    assert "prune" in calls
    assert "已清理 3 个" in out
def test_failed_deploy_does_not_prune(monkeypatch):
    """**失败时不能清理**：这时 rollback标签是刚需。"""
    calls = _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    monkeypatch.setattr(deploy, "prune_old_images",
        lambda h: (calls.append("prune"), "3")[1])
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert code !=0
    assert "prune" not in calls, "验收失败却清理了镜像"


def test_host_disk_pct_uses_df_semantics(monkeypatch):
    """**必须与 `df -P` 同口径**：`used/(used+avail)`。

    ext4 给 root 保留 5%，把它算进分母会让读数偏低 4 个多点——
    那正是 P0（95%）永远触发不了的原因（详见 jobs/cleanup.py 的教训）。
    """
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: "/dev/sda2 102400000 81920000 12288000 87% /\n")
    got = deploy.host_disk_pct("h")
    # 81920000/(81920000+12288000) = 86.96%
    assert abs(got - 86.96) < 0.1, got
    assert abs(got - 80.0) > 5, f"还在用 used/total：{got}"


def test_build_cache_prune_caps_total_size(monkeypatch):
    """**按总量封顶（`--keep-storage`），不按创建时间（`until`）。**

    `until` 判的是**创建时间**：这台机器一天发十几次版，缓存几乎都是新的，
    实测跑下来返回 `Total: 0B`——**磁盘越线时一个字节都收不回来**，白跑一趟。
    `--keep-storage` 判总量，跑完缓存上限就是给定值，行为可预测。

    另：**必须有 `-f`**。少了它 ssh 无 TTY，`builder prune` 会等确认
    然后 900s 超时抛异常，把一次成功的部署变成 traceback。
    """
    seen = []
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: (seen.append(" ".join(cmd)), "Total: 0B")[1])
    deploy.prune_build_cache("h", keep_gb=5)
    cmd = seen[0]
    assert "builder prune" in cmd
    assert "-f" in cmd.split() or "--force" in cmd, f"没有 -f，会卡在确认：{cmd}"
    assert "--keep-storage 5GB" in cmd, f"没按总量封顶：{cmd}"
    assert "until=" not in cmd, f"别再按创建时间判（实测收不回东西）：{cmd}"


def test_deploy_reclaims_build_cache_only_when_disk_is_high(monkeypatch, capsys):
    """**越线才动**：80% 以上才回收构建缓存。

    构建缓存无法按项目筛，会牵连同机其它项目；平时清纯属自找麻烦。
    """
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 96.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_gb=5: (calls.append("cache"), "9.9GB")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "cache" in calls, "磁盘96%却没回收构建缓存"
    assert "9.9GB" in capsys.readouterr().out


def test_deploy_does_not_reclaim_cache_when_disk_is_fine(monkeypatch):
    """磁盘没压力时**不该动**构建缓存。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 40.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_gb=5: (calls.append("cache"), "0B")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "cache" not in calls, "磁盘40%却清理了构建缓存"


def test_deploy_does_not_reclaim_cache_at_steady_state(monkeypatch):
    """**稳态 90% 绝不能清缓存**：本机常年 85~90%，这是"每次发版都满核重建"
    那条坑的回归锁。清缓存换磁盘 = 用 CPU 尖峰换几个 GB，方向就错了。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 90.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_gb=5: (calls.append("cache"), "9.9GB")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "cache" not in calls, "稳态90%就清缓存，会让每次发版都满核重建"


def test_failed_deploy_does_not_reclaim_cache(monkeypatch):
    """验收失败时不清理——那时 rollback标签是刚需。"""
    calls = _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 95.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_gb=5: (calls.append("cache"), "1GB")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) != 0
    assert "cache" not in calls


def test_host_disk_pct_returns_none_when_it_cannot_parse(monkeypatch):
    """**解析失败必须返回 `None`，不能是 0**。

    `_capture` 是 `stdout + stderr` 拼接，ssh 的警告/错误会混进来。
    0 会被当成"磁盘很空"，于是缓存回收被静默跳过、一行日志都没有——
    而这台机器的盘位常年在 85% 以上，恰恰最需要它工作。
    """
    for bad in [
        "",                                                    # 空输出
        "ssh: connect to host h port 22: Connection refused",  # ssh 挂了
        "Filesystem 1024-blocks Used Available Capacity Mounted on",  # df 头行
        "/dev/sda2 102400000 81920000",                        # 字段不足
        "/dev/sda2 102400000 x y 87% /",                        # 数字不合法
        "/dev/sda2 0 0 0 0% /",                                 # 分母为 0
    ]:
        monkeypatch.setattr(deploy, "_capture", lambda cmd, b=bad: b)
        assert deploy.host_disk_pct("h") is None, f"这个输入应该判为读不到：{bad!r}"


def test_host_disk_pct_ignores_stderr_appended_after_df(monkeypatch):
    """stderr 追加在 df 行之后时，**取最后一行会取错**——所以要校验列形状。"""
    mixed = ("/dev/sda2 102400000 81920000 12288000 87% /\n"
             "Warning: Permanently added 'h' (ED25519) to known hosts.\n")
    monkeypatch.setattr(deploy, "_capture", lambda cmd: mixed)
    # 最后一行没有 `87%` 这种形状 → 判为读不到，而不是把警告里的数字当数据
    assert deploy.host_disk_pct("h") is None


def test_cleanup_failure_does_not_flip_a_successful_deploy(monkeypatch, capsys):
    """**清理炸了不能把成功的部署变成失败。**

    `_capture` 是 `subprocess.run(..., timeout=900)`：ssh 卡住抛
    `TimeoutExpired`、ssh 不存在抛 `FileNotFoundError`。未捕获就是
    「部署成功却以非零码 + traceback 结束」，CI/值班会去回滚一个健康的新版本。
    """
    _patch_common(monkeypatch, verify="OK")

    def boom(host):
        raise TimeoutError("ssh 卡住了")

    monkeypatch.setattr(deploy, "prune_old_images", boom)
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "不影响本次部署" in capsys.readouterr().out


def test_cleanup_warns_when_cache_prune_reports_an_error(monkeypatch, capsys):
    """docker 挂了时最后一行是报错文本，**不能当回收量打出来**。"""
    _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 95.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
                        lambda h, keep_gb=5: "Cannot connect to the Docker daemon")
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    out = capsys.readouterr().out
    assert "回收失败" in out, out
    assert "构建缓存：Cannot" not in out, "把报错当成了回收量"


def _fake_archive(path):
    """造一个**真的** tar.gz：`main()` 里会 `tarfile.open()` 统计文件数。"""
    import tarfile as _tf

    with _tf.open(path, "w:gz") as tf:
        data = b"x"
        info = _tf.TarInfo("dummy.txt")
        info.size = len(data)
        import io as _io

        tf.addfile(info, _io.BytesIO(data))


def test_blocked_window_covers_backup_and_eod(monkeypatch):
    """**备份/EOD 窗口内必须拒绝部署**（可 --force-window 强推）。

    2026-11-24 的真实事故：23:30:14 部署、23:33:12 新容器起来，
    正好吃掉 23:30 的备份（`.market-20261124.db.tmp` 残留），
    **那一晚的备份永久丢失**，直到次日 08:55 才报一条 backup_stale。
    """
    from datetime import datetime as _dt

    for hhmm in ["23:30", "23:26", "23:49", "15:05", "15:30", "08:40", "09:00"]:
        h, m = map(int, hhmm.split(":"))
        reason = deploy.in_blocked_window(_dt(2026, 11, 24, h, m))
        assert reason, f"{hhmm} 应该被挡住（它会打断 EOD/备份），实际放行"


def test_outside_windows_deploys_are_allowed():
    """窗口之外不该拦——否则白天根本没法发版。"""
    from datetime import datetime as _dt

    for hhmm in ["10:00", "11:00", "14:00", "20:00", "23:00", "23:55", "07:00"]:
        h, m = map(int, hhmm.split(":"))
        assert deploy.in_blocked_window(_dt(2026, 11, 24, h, m)) is None, \
            f"{hhmm} 不该被拦住"


def test_dry_run_is_never_blocked(monkeypatch, capsys):
    """`--dry-run` 不重启任何东西，不该受窗口限制。"""
    from datetime import datetime as _dt

    monkeypatch.setattr(deploy, "in_blocked_window",
                        lambda now: "每日备份")
    monkeypatch.setattr(deploy, "build_archive", _fake_archive)
    # 工作区可能真的有改动（本仓库日常状态），用 --allow-dirty 隔离掉
    #
    # ⚠️ 取 HEAD 也要桩掉（2026-11-26）：本用例断言的是"窗口校验不影响
    # --dry-run"，与仓库状态无关。原来真去跑 `git rev-parse`，CI 的容器里
    # 有 git 却没有 `.git`，于是退出 128 —— 一条**窗口**用例被**仓库**环境
    # 拖红（main 上长期如此）。与被测行为无关的依赖一律桩掉。
    monkeypatch.setattr(deploy, "_sha", lambda: "deadbeef")
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--dry-run",
                        "--allow-dirty"]) == 0


def test_force_window_bypasses_the_check(monkeypatch, capsys):
    """`--force-window` 明确越过窗口检查，但必须打警告。"""
    _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "in_blocked_window", lambda now: "每日备份")
    rc = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0",
                      "--force-window"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "强推部署" in out, out


def test_capture_swallows_ssh_timeout_instead_of_raising(monkeypatch):
    """**`_capture` 必须自己吞掉 subprocess 异常。**

    `subprocess.run(..., timeout=900)`：ssh 卡住抛 `TimeoutExpired`、
    ssh 不存在抛 `FileNotFoundError`。不捕获的话 `main()` 以 traceback +
    退出码 1 结束——而 1 的语义是「确认坏、已回滚」，
    值班会去手工回滚一个其实健康的新版本。
    """
    import subprocess as _sp

    def boom(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(deploy.subprocess, "run", boom)
    out = deploy._capture(["ssh", "h", "true"])
    assert deploy.CAPTURE_FAILED in out
    assert "TimeoutExpired" in out

    def no_ssh(cmd, **kw):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(deploy.subprocess, "run", no_ssh)
    assert deploy.CAPTURE_FAILED in deploy._capture(["ssh", "h", "true"])


def test_capture_failure_maps_to_unknown_not_fail(monkeypatch):
    """**「没验成」必须是 UNKNOWN（退出 3、不回滚），不能是 FAIL。**

    FAIL 会触发自动回滚——把一次成功的部署强行切回旧版本，代价很大。
    """
    monkeypatch.setattr(deploy, "_capture",
                        lambda cmd: f"{deploy.CAPTURE_FAILED}TimeoutExpired: ssh 卡住")
    got = deploy.verify_deployment("h", "api", 10)
    assert got.startswith("UNKNOWN:"), got
    # 原因要能看出来是「拿不到输出」，不是笼统的「没有结论」
    assert "拿不到验收输出" in got, got


def test_drift_prompt_failure_does_not_fail_a_successful_deploy(monkeypatch, capsys):
    """配置漂移提示只是"提示"，拿不到不能翻掉已经成功的部署。"""
    _patch_common(monkeypatch, verify="OK")
    calls = []

    def cap(cmd):
        joined = " ".join(cmd)
        if "config_drift" in joined or "drift" in joined:
            calls.append("drift")
            return f"{deploy.CAPTURE_FAILED}TimeoutExpired"
        return ""

    monkeypatch.setattr(deploy, "_capture", cap)
    rc = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert rc == 0, "漂移提示失败把成功部署变成了失败"


def test_pre_upgrade_backup_does_not_overwrite_the_previous_one(monkeypatch):
    """**不可逆迁移前的备份不能每次都覆盖同一个文件。**

    原来写死 `data/market.db.pre-upgrade`。首次升级生成的"旧结构"备份，
    在**下一次**不可逆迁移时会被覆盖成"已含上次新结构"的库——
    此时想回退到更早那个点，原始备份已经没了。
    """
    import re

    seen = []
    monkeypatch.setattr(deploy, "_capture",
                        lambda cmd: (seen.append(" ".join(cmd)), "SS_BACKUP=OK")[1])
    deploy.backup_before_upgrade("h", "/srv/app")
    script = seen[0]

    # 目标文件名必须带时间戳
    m = re.search(r"market\.db\.pre-upgrade(-(\d{8}-\d{6}))?", script)
    assert m, f"命令里找不到备份文件名：{script[:300]}"
    assert m.group(2), f"备份文件名没有时间戳，会覆盖上一份：{m.group(0)}"
    # 并且要有限制份数的清理，避免 1.3GB × N 占满磁盘
    assert "tail -n +4" in script, "没有保留份数限制，磁盘会被备份撑满"


def test_pre_upgrade_backup_keeps_only_recent_three(monkeypatch):
    """保留策略是"最近 3 份"，不是无限累积。"""
    seen = []
    monkeypatch.setattr(deploy, "_capture",
                        lambda cmd: (seen.append(" ".join(cmd)), "SS_BACKUP=OK")[1])
    deploy.backup_before_upgrade("h", "/srv/app")
    assert "tail -n +4" in seen[0]


def test_build_command_pins_tag_and_project_name():
    """**`build`/`up` 命令必须 `env -u TAG` 并显式 `-p`。**

    镜像名是 `stock-screener:${TAG:-latest}`。运维 shell 里若
    `export TAG=v0.2.133`，`build api` 产出 `stock-screener:v0.2.133`，
    而 `_image_id()` 查的是 `stock-screener:latest`（旧镜像）→ 验收判 FAIL →
    回滚把 rollback tag 打到 latest 再 `up -d`，但 compose 仍按 TAG 拉**新镜像**，
    于是"回滚"之后跑的还是新版本，状态完全错乱。

    `-p` 同理：`COMPOSE_PROJECT_NAME` 被改会让容器名与 `verify_script`
    里拼的 `stock-screener-{svc}-1` 对不上，变成假失败。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 现在 compose 命令统一由 `compose_cmd()` 构造（正向部署与回滚共用），
    # 两处都去调用它——这里查那个唯一构造点。
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "compose_cmd")

    # 返回值表达式
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
    assert rets, "compose_cmd() 没有返回值"
    expr = ast.unparse(rets[0].value)
    assert "env -u TAG" in expr, f"没有 unset TAG：{expr}"
    assert "-p " in expr, f"没有显式指定项目名：{expr}"

    # **回滚必须用同一个构造点**（Codex 评审：只固定正向等于留了半边口子）
    rb = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "rollback")
    rb_src = ast.unparse(rb)
    assert "compose_cmd()" in rb_src, f"回滚没用 compose_cmd()，仍受 TAG 污染：{rb_src[:200]}"
    assert "env -u TAG" not in rb_src, "回滚自己拼了 compose 命令，会与正向分叉"


def test_deploy_refuses_when_another_deploy_holds_the_lock(monkeypatch, capsys):
    """**两个部署同时跑必须拦住。**

    后发者记录的「上一次验收通过镜像」可能是前发者尚未验完的镜像，
    失败回滚时就可能回到一个已知坏的版本。
    """
    _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "_acquire_deploy_lock", lambda host, path: None)
    rc = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert rc == 1, "锁被占时应该拒绝部署"
    # 提示文本由 `_acquire_deploy_lock()` 自己打印（见
    # `test_lock_refuses_when_flock_reports_busy`），这里只断言"拒绝"这个行为，
    # 否则把提示文案绑死在调用方，改文案就会连带弄红两条用例。


def _stdout_pipe(lines):
    """造一个**真管道**冒充 ssh 的 stdout。

    `_acquire_deploy_lock` 用 `select.select([proc.stdout], ...)` 加读超时，
    而 `StringIO` 没有 `fileno()`，`select` 会直接抛 → 被当成"拿不到锁"。
    所以桩必须用真的 fd（写完就关写端，读端读完后自然 EOF）。
    """
    import os

    r, w = os.pipe()
    try:
        os.write(w, "".join(lines).encode())
    finally:
        os.close(w)
    return os.fdopen(r, "r", encoding="utf-8", errors="replace")


class FakeLockProc:
    """假装是那条持有 `flock` 的 ssh 子进程。"""

    def __init__(self, first_line: str = "SS_LOCK=GOT"):
        import io

        self.stdin = io.StringIO()
        self._line = first_line
        self.waited = False
        self.killed = False
        self.stdout = self

    def readline(self):
        return self._line + "\n"

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def poll(self):
        """还活着 → 返回 None（`_lock_lost` 靠它判断）。"""
        return None

    def kill(self):
        self.killed = True


def test_deploy_releases_the_lock_even_when_verification_fails(monkeypatch):
    """**验收失败也必须释放锁**，否则下一次部署永远被自己挡住。"""
    _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    released = []
    monkeypatch.setattr(deploy, "_acquire_deploy_lock", lambda host, path: FakeLockProc())
    monkeypatch.setattr(deploy, "_release_deploy_lock",
                        lambda holder: released.append(holder))
    deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert len(released) == 1, f"部署失败后没释放锁：{released}"


def test_lock_uses_flock_on_a_long_lived_session(monkeypatch):
    """**锁必须是内核 `flock`，不是"锁文件 + mtime 陈旧判定"。**

    旧做法是 `O_CREAT|O_EXCL` + 40 分钟 mtime 判定，抢占要「读 mtime → 删 →
    重建」三步，两个进程可以都判陈旧、都重建成功，各自以为自己拿到了锁
    （**TOCTOU**）；释放端「读内容 → 比 → 删」同理，慢部署结束时会删掉
    后来者的锁。

    新做法：一条 ssh 常驻持有 fd 9 上的 `flock`，远端脚本阻塞在 `read` 上
    不退出；本地关 stdin → 远端 EOF → 进程退出 → 内核释放。
    没有 TOCTOU，也没有"陈旧锁"。
    """
    captured = {}

    class Fake:
        def __init__(self, *a, **kw):
            captured["argv"] = a[0] if a else kw.get("args")
            captured["kwargs"] = kw
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe(["SS_LOCK=GOT\n"])
            self.stderr = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    got = deploy._acquire_deploy_lock("h", "/srv/app/.deploy.lock")
    assert got is not None, "拿到锁却返回了 None"
    argv = captured["argv"]
    script = argv[-1]
    assert "flock -n 9" in script, f"没用 flock：{script}"
    # 必须持锁：阻塞读，不能拿完就退出
    assert "read -r" in script, f"拿完就退出，锁等于没有：{script}"

    # ⚠️ **`stdin=PIPE` 才是"持锁"的关键不变量**。改成 DEVNULL 时远端 `read`
    # 立刻 EOF、脚本在 `echo SS_LOCK=GOT` 之后就退出，锁在拿到的一瞬间就释放，
    # 互斥等于没有——而只断言脚本文本的话，这个变异**测不出来**。
    import subprocess as _sp

    assert captured["kwargs"].get("stdin") is _sp.PIPE, \
        f"锁连接没有 stdin=PIPE，远端会立刻 EOF 释放锁：{captured['kwargs']}"

    # 空闲连接必须保活：NAT/防火墙的空闲超时 5–30 分钟，
    # 而首次构建十几分钟 + 验收 90 秒，这条最闲的连接最容易被悄悄丢掉。
    joined = " ".join(argv)
    assert "ServerAliveInterval" in joined, \
        f"持锁连接没有保活，会被中间设备悄悄断掉：{joined}"

    # 锁路径必须走**位置参数**，不能拼进脚本正文：
    # 路径含 `"`/`$`/反引号时，拼进去会被远端 shell 解析（也会直接语法错）。
    # 判据：路径在整条远端命令里只出现**一次**（就是那个位置参数），
    # 脚本正文里用的是 `"$1"`。
    assert joined.count("/srv/app/.deploy.lock") == 1, (
        f"锁路径被拼进脚本正文了（应只作为位置参数出现一次）：{joined}")
    assert '"$1"' in joined, f"脚本没有用位置参数取锁路径：{joined}"


def test_lock_refuses_when_flock_reports_busy(monkeypatch):
    """`flock -n` 失败即 BUSY —— 必须当成"别人在部署"并中止。"""
    class Fake:
        def __init__(self, *a, **kw):
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe(["SS_LOCK=BUSY\n"])
            self.stderr = None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    assert deploy._acquire_deploy_lock("h", "/l") is None


def test_lock_failure_never_falls_back_to_unlocked_deploy(monkeypatch, capsys):
    """**拿不到锁一律中止**，不能"继续部署、并发自行避免"。

    旧实现在拿不到锁（如目标机缺 python3）时会把 token 置空并**继续部署**，
    打印一句"并发需自行避免"——那句话等于没有锁。
    """
    class Fake:
        def __init__(self, *a, **kw):
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe([])          # 没有任何输出
            self.stderr = None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    assert deploy._acquire_deploy_lock("h", "/l") is None
    out = capsys.readouterr().out
    assert "拒绝部署" in out, out


def test_lock_output_is_matched_as_a_whole_line_not_substring(monkeypatch):
    """**traceback 回显里的 `SS_LOCK=BUSY` 不能当成真的 BUSY。**

    真实故障：锁脚本用一行式 `try:` 拼（语法错误），traceback 把源码行回显，
    而源码里含 `SS_LOCK=BUSY` 字面量，子串匹配就把它当"锁被占"——
    **部署被一个根本不存在的锁拦住了**。现在只认整行等于 `SS_LOCK=GOT`。
    """
    class Fake:
        def __init__(self, *a, **kw):
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe([
                '  File "<string>", line 15\n',
                "    print('SS_LOCK=BUSY')\n",
                "SyntaxError: invalid syntax\n"])
            self.stderr = None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    assert deploy._acquire_deploy_lock("h", "/l") is None, "把 traceback 回显当成了拿到锁"


def test_backup_failure_text_must_not_contain_command_source(monkeypatch):
    """**Codex 抓到的阻断**：异常文本里带命令原文 → 备份被误判成功。

    `TimeoutExpired.__str__` 是 `Command '[...]' timed out`，而 `cmd`
    **包含 bash 脚本全文**，脚本里就有 `SS_BACKUP=OK` 这个字面量。
    于是 `"SS_BACKUP=OK" not in out` 为 False → 返回 "OK" →
    **放行一次没有备份的不可逆迁移**。
    """
    import subprocess as _sp

    def boom(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(deploy.subprocess, "run", boom)
    got = deploy.backup_before_upgrade("h", "/srv/app")
    assert got.startswith("FAIL:"), f"备份超时被当成成功了：{got}"


def test_capture_failure_text_never_includes_the_command(monkeypatch):
    """哨兵后面**只跟异常类型名**，不带 `str(exc)`（那会带出整条命令）。"""
    import subprocess as _sp

    secret_marker = "SS_BACKUP=OK"

    def boom(cmd, **kw):
        # 模拟真实情况：cmd 里含成功标记字面量
        raise _sp.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(deploy.subprocess, "run", boom)
    out = deploy._capture(["ssh", "h", "bash -c \"echo SS_BACKUP=OK\""])
    assert deploy.CAPTURE_FAILED in out
    assert "SS_BACKUP=OK" not in out, f"异常文本把命令原文带出来了：{out}"


def test_backup_marker_must_be_a_standalone_line(monkeypatch):
    """成功标记必须**整行相等**，不能被"回显出来的命令原文"蒙过。"""
    monkeypatch.setattr(deploy, "_capture",
                        lambda cmd: "bash: echo SS_BACKUP=OK: command not found\n")
    got = deploy.backup_before_upgrade("h", "/srv/app")
    assert got.startswith("FAIL:"), f"非独立行也被当成成功：{got}"

    monkeypatch.setattr(deploy, "_capture", lambda cmd: "noise\nSS_BACKUP=OK\n")
    assert deploy.backup_before_upgrade("h", "/srv/app") == "OK"


def test_window_is_rechecked_right_before_recreate(monkeypatch, capsys):
    """**重建前必须再查一次窗口**（Codex 两轮都点这条）。

    入口检查不够：23:24 通过，构建跑到 23:31，`up -d --force-recreate`
    就直接把 jobs 重启了，而备份正在拷 1.3GB。`stop_grace_period: 180s`
    也救不了——备份要 8 分钟。2026-11-24 就是这么丢的备份。
    """
    _patch_common(monkeypatch, verify="OK")
    calls = {"n": 0}

    def window(now):
        calls["n"] += 1
        # 第一次（入口）放行，第二次（重建前）已在窗口里
        return None if calls["n"] == 1 else "每日备份（23:30 起，约 8 分钟）"

    monkeypatch.setattr(deploy, "in_blocked_window", window)
    rc = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert rc == 1, "重建前进窗口时应该中止"
    out = capsys.readouterr().out
    assert "中止部署" in out and "重建前" in out, out
    assert calls["n"] >= 2, "只查了一次窗口，重建前没复查"


def test_window_abort_does_not_trigger_rollback(monkeypatch):
    """窗口中止时容器**没动**，不该回滚（回滚会把健康版本换掉）。"""
    _patch_common(monkeypatch, verify="OK")
    calls = {"n": 0}

    def window(now):
        calls["n"] += 1
        return None if calls["n"] == 1 else "每日备份"

    monkeypatch.setattr(deploy, "in_blocked_window", window)
    ro = []
    monkeypatch.setattr(deploy, "rollback",
                        lambda h, d, s: (ro.append(1), (True, "OK"))[1])
    deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert ro == [], "窗口中止后不该回滚"


def test_window_check_uses_shanghai_time_not_the_local_machine():
    """**窗口判断必须用上海时间，不能依赖跑脚本那台机器的时区。**

    真实踩坑：测试容器是 UTC，`datetime.now()` 在北京时间 16:40 返回 08:40，
    正好落进「盘前 08:35–09:10」窗口 —— 于是所有部署用例都被莫名拦下，
    而生产（在上海的笔记本上跑）一切正常。这种环境相关的差异极难排查。
    """
    from zoneinfo import ZoneInfo

    import datetime as _dt

    now_sh = _dt.datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    got = deploy._sh_now()
    # 微秒会流逝，比较容差即可（重点是**不是 UTC**：差 8 小时会立刻暴露）
    assert abs((got - now_sh).total_seconds()) < 5, \
        f"_sh_now() 返回的不是上海时间：{got} vs {now_sh}"


def test_sh_now_is_timezone_independent(monkeypatch):
    """把进程时区改成 UTC 后，`_sh_now()` 仍然给上海时间。"""
    import os
    import time as _time
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("TZ", "UTC")
    _time.tzset()
    try:
        import datetime as _dt

        expect = _dt.datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        got = deploy._sh_now()
        assert abs((got - expect).total_seconds()) < 5, f"UTC 下拿到了 {got}"
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()
def test_every_main_call_stubs_the_block_window():
    """**凡调用 `deploy.main()` 的用例，都必须桩掉危险窗口。**

    2026-11-26：`test_build_failure_also_rolls_back` 漏了这条桩，而 CI 恰好
    在 `(8,35)~(9,10)` 窗口里跑（容器是 UTC），部署被拒、`_run` 一次都没调用，
    于是报「构建失败但没有回滚」。**一天必红 35 分钟**的用例，等于这条门禁
    根本不存在——而且红的时候指向的是"回滚逻辑坏了"，带人去查错的地方。

    判据落在机制上：不是记住"下次别忘了"，而是每次都机械核对一遍。
    走 `_patch_common()` 的自动带上这条桩，手写的必须自己写。
    """
    import ast
    import inspect

    src = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        body_src = ast.get_source_segment(src, node) or ""
        if "deploy.main([" not in body_src:
            continue
        if ("in_blocked_window" in body_src
                or "_patch_common(" in body_src
                or "make_args" in body_src):
            continue
        offenders.append(node.name)
    assert not offenders, (
        f"这些用例调了 deploy.main() 却没桩危险窗口，"
        f"在 (8,35)~(9,10) 那 35 分钟里必红：{offenders}")


def test_warns_about_services_that_the_deploy_will_not_recreate():
    """**部署没重建的服务必须说出来。**

    2026-11-26 真坑：给 `cloudflared` 改了 compose（加 metrics / 加 protocol），
    跑 `deploy.py` 显示「验收通过」，但 `cloudflared` **根本没被重建**——
    `--services` 默认只含 api/jobs/agent。git 里的 compose 与实际运行的容器
    长期不一致，而部署日志一个字都没提。

    更糟的是手工补重建时漏了 `--project-directory .`，`./cf` 被解析成
    `deploy/cf`（空目录），容器读不到 `config.yml`，**整站 1033**。
    所以提示必须带**完整命令**，不能只说"记得重建一下"。
    """
    skipped = deploy.composed_services_not_recreated("api jobs agent")
    assert "cloudflared" in skipped, f"没提示 cloudflared 没被重建：{skipped}"
    assert "ntfy" in skipped, f"没提示 ntfy 没被重建：{skipped}"
    assert "api" not in skipped, "把已经重建的服务也算进未重建了"
    # 不能把 compose 里 logging 之类的子键当成服务
    assert "options" not in skipped, f"把子键当成服务了：{skipped}"

    # 全量重建时不该有任何"未重建"
    assert deploy.composed_services_not_recreated(
        "api jobs agent ntfy backfill cloudflared") == []


def test_deploy_prints_the_full_manual_command_with_project_directory(monkeypatch, capsys):
    """提示里的命令必须带 `--project-directory .`。

    漏了它，`./cf` 会解析成 `deploy/cf`（空目录）→ cloudflared 读不到配置
    → 整站 1033。这是当天真实发生的第二次故障。
    """
    _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "in_blocked_window", lambda now: None)
    rc = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert rc == 0, f"部署应成功，实际 {rc}"
    out = capsys.readouterr().out
    assert "没有生效" in out, out
    assert "cloudflared" in out, out

    # **必须断言「命令那一行」**，不能只看整个输出。
    # 提示语的散文部分本身就写着 `--project-directory .**`（在解释为什么需要它），
    # 于是 `"--project-directory ." in out` 在命令-line 丢了参数时**照样通过**
    # —— 实测把命令里的参数删掉后，弱断言仍然绿。要精确到以 `cd ` 开头那行。
    cmd_lines = [ln for ln in out.splitlines() if ln.strip().startswith("cd ")]
    assert cmd_lines, f"没有打印手工重建命令：{out}"
    cmd = cmd_lines[0]
    assert "--project-directory ." in cmd, (
        f"命令漏了 --project-directory .，./cf 会解析成 deploy/cf → 整站 1033：{cmd}")
    assert "force-recreate" in cmd and "cloudflared" in cmd, cmd


def test_shutdown_lock_proc_closes_stdin_so_the_remote_releases(monkeypatch):
    """释放锁 = **关掉 stdin**，让远端 `read` 读到 EOF 后退出。

    这条路径之前完全没有直接覆盖（`test_deploy_releases_the_lock...` 把
    `_release_deploy_lock` 整个桩掉了，只证明 `finally` 被执行，不证明
    stdin 真的被关）。stdin 没关的话远端会一直挂着——锁永远不释放。
    """
    class Proc:
        def __init__(self):
            import io
            self.stdin = io.StringIO()
            self.waited = False
            self.killed = False
        def wait(self, timeout=None):
            self.waited = True
            return 0
        def kill(self):
            self.killed = True

    p = Proc()
    deploy._shutdown_lock_proc(p)
    assert p.stdin.closed, "没有关闭 stdin，远端会一直持锁"
    assert p.waited and not p.killed


def test_shutdown_lock_proc_kills_a_hung_connection():
    """远端不响应（wait 超时）时必须 kill，不能卡在释放上。"""
    import subprocess as _sp

    class Proc:
        def __init__(self):
            import io
            self.stdin = io.StringIO()
            self.kills = 0
        def wait(self, timeout=None):
            if self.kills == 0:
                raise _sp.TimeoutExpired("ssh", timeout)
            return 0
        def kill(self):
            self.kills += 1

    p = Proc()
    deploy._shutdown_lock_proc(p)
    assert p.kills == 1, "超时后没有 kill，释放会一直卡住"


def test_shutdown_lock_proc_tolerates_none():
    deploy._shutdown_lock_proc(None)      # 不该抛


def test_lock_timeout_when_remote_never_answers(monkeypatch, capsys):
    """**远端一直不回应必须超时中止**，不能无限阻塞。

    远端可能在 `flock` 成功之后、`echo` 之前卡住（hung mount、
    `~/.ssh/rc` 里挂住）。此时一行提示都没有，人只会以为"在构建"，
    而远端**可能已经持着锁**，把别人也一起挡住。
    """
    class Silent:
        def __init__(self, *a, **kw):
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe([])     # 立刻 EOF，等价于"没有结果"
            self.stderr = None
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Silent)
    monkeypatch.setattr(deploy, "LOCK_RESULT_TIMEOUT_S", 1)
    assert deploy._acquire_deploy_lock("h", "/l") is None
    assert "没有拿到锁结果" in capsys.readouterr().out


def test_lock_ignores_remote_stdout_noise_before_the_result(monkeypatch):
    """`~/.ssh/rc` 之类写进 stdout 的噪声不能让我们误判"没拿到锁"。

    判据必须只认**整行等于** `SS_LOCK=GOT`，噪声行要跳过并回报，
    而不是拿第一行去比。
    """
    class Noisy:
        def __init__(self, *a, **kw):
            import io
            self.stdin = io.StringIO()
            self.stdout = _stdout_pipe([
                "Welcome to the box\n",
                "\x1b[1;32mnotice\x1b[0m: rc script ran\n",
                "SS_LOCK=GOT\n"])
            self.stderr = None
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Noisy)
    got = deploy._acquire_deploy_lock("h", "/l")
    assert got is not None, "远端有噪声输出时拿不到锁"


def test_lock_end_to_end_against_a_real_flock(monkeypatch):
    """**真跑一次 flock**：验证"持锁期间第二个拿不到、关了 stdin 就释放"。

    前面几条都是桩 Popen，验的是"我们有没有把参数传对"。这条用本地 `bash`
    真起一次同款脚本，验的是**这套手法本身成立**——`flock` 挂在 fd 9 上、
    子进程 `flock` 退出后锁仍由 shell 持着、stdin 关闭即释放。
    """
    import io
    import os
    import random
    import subprocess as _sp

    import pytest

    if os.name != "posix":
        pytest.skip("flock 只在 POSIX 上")
    if _sp.run(["bash", "-c", "command -v flock"], capture_output=True).returncode:
        pytest.skip("环境没有 flock")

    lock = f"/tmp/ds-lock-{random.randint(1, 10**9)}.lock"
    script = "\n".join([
        "set -u",
        'mkdir -p "$(dirname "$1")" 2>/dev/null || true',
        'exec 9>"$1" || { echo SS_LOCK=NOFILE; exit 0; }',
        "flock -n 9 || { echo SS_LOCK=BUSY; exit 0; }",
        "echo SS_LOCK=GOT",
        "while IFS= read -r _line; do :; done",
    ])

    def spawn():
        return _sp.Popen(["bash", "-c", script, "bash", lock],
                         stdin=_sp.PIPE, stdout=_sp.PIPE,
                         stderr=_sp.DEVNULL, text=True)

    p1 = spawn()
    assert p1.stdout.readline().strip() == "SS_LOCK=GOT"
    p2 = spawn()
    assert p2.stdout.readline().strip() == "SS_LOCK=BUSY", "第二个也拿到了锁！"
    p2.wait(timeout=10)

    p1.stdin.close()                      # 释放
    p1.wait(timeout=10)
    p3 = spawn()
    assert p3.stdout.readline().strip() == "SS_LOCK=GOT", "释放后拿不到锁"
    p3.stdin.close()
    p3.wait(timeout=10)
    os.unlink(lock)


def test_lock_remote_read_has_a_timeout_backed_by_a_heartbeat(monkeypatch):
    """**远端 `read` 必须有超时，本地必须有心跳。**

    Codex 第三轮指出：本地机器失联（网络分区、休眠）时，服务端可能迟迟收不到
    FIN，远端 `read` 会一直阻塞——**锁就永远不释放**。

    但只加超时更糟：一次超过超时的正常部署会**被远端自己释放锁**，本地还在
    构建，正好又变成"两个部署能并行"。所以超时必须由心跳续期。
    """
    captured = {}

    class Fake:
        def __init__(self, *a, **kw):
            captured["argv"] = a[0]
            self.stdin = __import__("io").StringIO()
            self.stdout = _stdout_pipe(["SS_LOCK=GOT\n"])
            self.stderr = None
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    h = deploy._acquire_deploy_lock("h", "/l")
    assert h is not None
    script = " ".join(captured["argv"])
    assert "read -r -t" in script, f"远端 read 没有超时，失联时锁永不释放：{script}"
    # 超时必须**明显大于**心跳间隔，否则正常部署会被误判超时
    assert deploy.LOCK_HEARTBEAT_S * 2 <= 180, (
        f"心跳 {deploy.LOCK_HEARTBEAT_S}s 相对远端 180s 超时太接近，容易误释放")


def test_lock_heartbeat_actually_writes_keepalive(monkeypatch):
    """心跳要真的往持锁连接写东西，否则远端 180 秒就自己释放锁。"""
    import io
    import time as _t

    class Proc:
        def __init__(self):
            self.stdin = io.StringIO()

    monkeypatch.setattr(deploy, "LOCK_HEARTBEAT_S", 0.05)
    p = Proc()
    deploy._start_lock_heartbeat(p)
    assert getattr(p, "_hb_stop", None) is not None, "启动心跳没留下停止句柄"
    _t.sleep(0.2)
    deploy._shutdown_lock_proc(p) if hasattr(p, "wait") else None
    assert p.stdin.getvalue().count("\n") >= 1, "心跳没有写出任何东西"


def test_lock_survives_a_partial_line_instead_of_hanging(monkeypatch):
    """**只收到半行时不能死等换行**（Codex 第三轮指出）。

    `select` 只保证"有数据可读"，不保证"有一个完整行"。远端只送来
    `SS_LOCK=GO`（没有换行）时，`readline()` 会在 `select` 返回之后继续阻塞，
    **30 秒超时形同虚设**——而这是拿锁路径，卡住等于锁被永久占住。
    """
    class Partial:
        def __init__(self, *a, **kw):
            self.stdin = __import__("io").StringIO()
            self.stdout = _stdout_pipe(["SS_LOCK=GO"])     # 没有换行
            self.stderr = None
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Partial)
    monkeypatch.setattr(deploy, "LOCK_RESULT_TIMEOUT_S", 1)
    started = __import__("time").monotonic()
    assert deploy._acquire_deploy_lock("h", "/l") is None
    elapsed = __import__("time").monotonic() - started
    assert elapsed < 5, f"半行把拿锁卡住了 {elapsed:.1f} 秒"


def test_acquiring_the_lock_actually_starts_the_heartbeat(monkeypatch):
    """**拿锁时必须真的把心跳启动起来。**

    单独测 `_start_lock_heartbeat()` 只能证明"这个函数写对了"，证明不了
    "它被调用了"。而漏掉这个调用时，远端 `read -t 180` 会在**每次超过 3 分钟
    的部署**里自己释放锁——本地还在构建，正好又是两个部署并行。
    变异验证：删掉 `_acquire_deploy_lock` 里那一行调用，本用例会红。
    """
    class Fake:
        def __init__(self, *a, **kw):
            self.stdin = __import__("io").StringIO()
            self.stdout = _stdout_pipe(["SS_LOCK=GOT\n"])
            self.stderr = None
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    monkeypatch.setattr(deploy.subprocess, "Popen", Fake)
    h = deploy._acquire_deploy_lock("h", "/l")
    assert h is not None
    assert getattr(h, "_hb_stop", None) is not None, \
        "拿到锁了但没启动心跳，远端 180 秒后会自己释放锁"
    deploy._shutdown_lock_proc(h)
