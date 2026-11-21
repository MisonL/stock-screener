"""部署验收与回滚的测试。

背景：v0.2.114那次 jobs容器重启循环，`deploy.py`打印一句
「下一步请自行核对 /healthz」就退出0——部署是否成功全靠人看。
这里把「重启后自动验收 +失败回滚」钉住。
"""
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy

twoId = "{{.Id}}"
fourId = "{{{{"


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
        lambda h, d, i: calls.append("record:" + i))
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


def test_verify_script_is_valid_bash_and_waits_for_stability(tmp_path):
    """验收脚本必须合法，且**不能一看到 running就下结论**。

    评审实测过旧版：`up -d`返回时容器就是 `Up0 seconds`，「三个都 running」
    第一轮就成立 →0.8秒返回 OK，healthcheck和启动期崩溃一个都等不到。
    """
    sh = tmp_path / "v.sh"
    sh.write_text(deploy.verify_script("api jobs agent",90,20,"sha256:x"))
    out = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
    assert out.returncode ==0, f"验收脚本不是合法 bash：{out.stderr}"

    body = sh.read_text()
    #1)先无条件等 settle（这是旧版最致命的缺陷）
    assert "settle" in body, "没有 settle阶段——会在启动后几百毫秒就下结论"
    assert "-lt 20" in body, f"settle没生效：{body[:400]}"
    #2)等健康检查
    assert "health:" in body and "unhealthy" in body, "没等 healthcheck"
    #3)比对镜像 ID
    assert "EXPECT" in body and ".Image" in body, "没校验跑的是不是新镜像"
    #4)扫 traceback，且**整行匹配**成功标记
    assert "Traceback (most recent call last)" in body
    assert "grep -qx" in body, "容器名不是整行匹配——子串会误匹配"
    assert "--since 4m" in body, "日志窗口被改小了"


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
        # **包含 OK但当行不是 OK**：上次成功的日志、被拼进详情的字符串
        ("SS_VERIFY=FAIL:上次是 SS_VERIFY=OK", "FAIL"),
        ("prefix SS_VERIFY=OK", "UNKNOWN"),
    ]
    for out, want in cases:
        monkeypatch.setattr(deploy, "_capture", lambda cmd, o=out: o)
        got = deploy.verify_deployment("h", "api jobs agent",1)
        assert got.startswith(want), f"输出 {out!r}期望 {want!r}，实际 {got!r}"


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
    """**「没验成」不能当成「部署坏了」去回滚**（评审 S3）。

    ssh抖动、脚本异常都会返回 UNKNOWN。自动回滚会把一次成功的部署
    强行切回旧版本并 recreate生产容器——代价远大于报警。
    """
    calls = _patch_common(monkeypatch,
        verify="UNKNOWN:验收脚本没有给出结论（ssh失败或脚本异常）")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0, "无法确认状态时不能报告成功"
    assert "rollback" not in calls, "「没验成」触发了回滚——会把好部署切回旧版"
    assert "不自动回滚" in out


def test_success_records_the_good_image(monkeypatch, capsys):
    """验收通过才记录"好镜像"，供下次当回滚点（避免坏镜像被快照）。"""
    calls = _patch_common(monkeypatch, verify="OK")
    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    assert code ==0
    assert "record:sha256:new" in calls, f"没记录好镜像：{calls}"
    assert "rollback" not in calls


def test_image_id_uses_a_valid_docker_format(monkeypatch):
    """**docker --format只能是两个花括号**。

    `{{{{.Id}}}}`会被 docker当成非法 format，命令失败返回空串，
    于是 `if image_id:`恒假——**marker永远写不进去**，
    回滚点退回「当前 latest」的老逻辑（等于没修）。

    pyflakes查不出这种错（在字符串里），必须靠测试钉。
    """
    seen = []

    def cap(cmd):
        seen.append(" ".join(cmd))
        return "sha256:abc123"

    monkeypatch.setattr(deploy, "_capture", cap)
    got = deploy._image_id("h")

    assert got == "sha256:abc123", f"没解析出镜像ID：{got!r}"
    assert twoId in seen[0], f"docker format不是两个花括号：{seen[0]}"
    assert fourId not in seen[0], "用了四个花括号——docker会拒绝"


def test_image_id_returns_empty_when_docker_fails(monkeypatch):
    """取不到镜像 ID时返回空串（调用方据此跳过比对）。"""
    monkeypatch.setattr(deploy, "_capture", lambda cmd: "Error: No such image")
    assert deploy._image_id("h") == ""


def test_snapshot_prefers_the_recorded_good_image(monkeypatch):
    """快照必须**优先**用 `.last_good_image`，而不是当前 latest。

    若上次回滚失败，latest停在坏镜像上；再从 latest快照，
    就把已知坏版本变成了回滚点，之后每次都「成功回滚」到起不来的版本。
    """
    seen = []

    def cap(cmd):
        seen.append(" ".join(cmd))
        return "SS_SNAPSHOT=MARKER"

    monkeypatch.setattr(deploy, "_capture", cap)
    mode = deploy.snapshot_image("h", "/srv/app")

    assert mode == "MARKER"
    cmd = seen[0]
    assert ".last_good_image" in cmd, "没用 marker文件"
    assert cmd.index(".last_good_image") < cmd.index("stock-screener:latest"), (
         "先用了 latest——坏镜像会被快照成回滚点")


def test_build_failure_also_rolls_back(monkeypatch, capsys):
    """**build/up失败也要回滚**（Codex两次评审都点了这条）。

    原来 `_run`抛 SystemExit直接退出，比验收早一步，回滚只在验收分支里。
    后果：`up -d`半途失败会留下「一半旧一半新」的栈，且无人回退。
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
    def no_verify(*args, **kwargs):
        raise AssertionError("build失败不该走到验收")
    monkeypatch.setattr(deploy, "verify_deployment", no_verify)

    code = deploy.main(["--host", "h", "--dir", "/srv/app", "--settle", "0"])
    out = capsys.readouterr().out
    assert code !=0, "构建失败却返回成功"
    assert "rollback" in calls, "构建失败但没有回滚——会留下半新半旧的栈"
    assert "构建或重启失败" in out
