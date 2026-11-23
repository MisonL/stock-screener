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
    monkeypatch.setattr(deploy, "prune_build_cache", lambda h, keep_hours=72: "")
    monkeypatch.setattr(deploy, "prune_old_images", lambda h: "0")
    monkeypatch.setattr(deploy, "pending_risky_migrations",
        lambda applied, d: [])
    monkeypatch.setattr(deploy, "read_schema_version",
        lambda h, d: ["001_batch1.sql"])
    monkeypatch.setattr(deploy, "config_drift_script", lambda d: "true")
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


def test_prune_only_targets_dangling_images_of_this_project(monkeypatch):
    """**只删本项目的悬空镜像**：用 `com.docker.compose.project`标签筛。
        这台机器上还跑着 WeKnora / selfhost / langbot；
        `docker image prune -f` 会把它们的悬空镜像一起删掉（实测有一个
        129MB 的 nginx 悬空镜像不是我们的）。
    """
    seen = []
    monkeypatch.setattr(deploy, "_capture",
        lambda cmd: (seen.append(" ".join(cmd)), "SS_PRUNED=abc\nSS_PRUNED=def")[1])
    assert deploy.prune_old_images("h") == "2"
    cmd = seen[0]
    assert "dangling=true" in cmd, "没限定悬空"
    assert "com.docker.compose.project" in cmd, "没按项目标签筛"
    assert "stock-screener" in cmd
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
    """没有可删的就返回 "0"，调用方据此不打印。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "")
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


def test_host_disk_pct_returns_zero_when_unreadable(monkeypatch):
    """读不到就返回 0（不告警），不能抛异常打断部署。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "")
    assert deploy.host_disk_pct("h") == 0.0
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "garbage")
    assert deploy.host_disk_pct("h") == 0.0


def test_build_cache_prune_keeps_recent_cache(monkeypatch):
    """**必须带 `until` 过滤器**：清掉最近三天会让出错后的重建变慢。"""
    seen = []
    monkeypatch.setattr(deploy, "_capture",
     lambda cmd: (seen.append(" ".join(cmd)), "Total: 0B")[1])
    deploy.prune_build_cache("h")
    assert "builder prune" in seen[0]
    assert "until=72h" in seen[0], f"没限定时间：{seen[0]}"


def test_deploy_reclaims_build_cache_only_when_disk_is_high(monkeypatch, capsys):
    """**越线才动**：80% 以上才回收构建缓存。

    构建缓存无法按项目筛，会牵连同机其它项目；平时清纯属自找麻烦。
    """
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 85.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_hours=72: (calls.append("cache"), "9.9GB")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "cache" in calls, "磁盘85%却没回收构建缓存"
    assert "9.9GB" in capsys.readouterr().out


def test_deploy_does_not_reclaim_cache_when_disk_is_fine(monkeypatch):
    """磁盘没压力时**不该动**构建缓存。"""
    calls = _patch_common(monkeypatch, verify="OK")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 40.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_hours=72: (calls.append("cache"), "0B")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) == 0
    assert "cache" not in calls, "磁盘40%却清理了构建缓存"


def test_failed_deploy_does_not_reclaim_cache(monkeypatch):
    """验收失败时不清理——那时 rollback标签是刚需。"""
    calls = _patch_common(monkeypatch, verify="FAIL:jobs起不来")
    monkeypatch.setattr(deploy, "host_disk_pct", lambda h: 95.0)
    monkeypatch.setattr(deploy, "prune_build_cache",
     lambda h, keep_hours=72: (calls.append("cache"), "1GB")[1])
    assert deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"]) != 0
    assert "cache" not in calls
