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
    monkeypatch.setattr(deploy, "rollback", fake_rollback)
    def verify_after_rollback(*args, **kwargs):
        #回滚之后**必须**再验一次（M6）；回滚之前不该验收
        assert "rollback" in calls, "验收跑在回滚之前了"
        return "OK"
    monkeypatch.setattr(deploy, "verify_deployment", verify_after_rollback)

    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0, "构建失败却返回成功"
    assert "rollback" in calls, "构建失败但没有回滚——会留下半新半旧的栈"
    assert "构建或重启失败" in out

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
