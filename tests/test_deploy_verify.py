"""部署验收与回滚的测试（v0.2.116）。

背景：v0.2.114那次 jobs容器重启循环，`deploy.py`打印一句
「下一步请自行核对 /healthz」就退出0——**部署是否成功全靠人看**。
这里把「重启后自动验收 +失败回滚」钉住。
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


def _patch_common(monkeypatch, verify="OK", rollback_ok=False, calls=None):
    """把 main()里所有会碰外部的动作都替身掉。"""
    if calls is None:
        calls = []
    monkeypatch.setattr(deploy, "check_clean", lambda **k: None)
    monkeypatch.setattr(deploy, "_sha", lambda: "deadbeef")
    monkeypatch.setattr(deploy, "_has_tag", lambda: False)
    monkeypatch.setattr(deploy, "build_archive", _noop_archive)
    monkeypatch.setattr(deploy, "snapshot_image", lambda host: calls.append("snapshot"))
    monkeypatch.setattr(deploy, "_run",
        lambda cmd: calls.append("run:" + " ".join(cmd)))
    monkeypatch.setattr(deploy, "config_drift_script", lambda d: "true")
    monkeypatch.setattr(deploy, "verify_deployment", lambda h, s, w: verify)
    def _rb(h, d, s):
        calls.append("rollback")
        return rollback_ok
    monkeypatch.setattr(deploy, "rollback", _rb)
    return calls


def test_verify_script_is_valid_bash_and_checks_the_right_things(tmp_path):
    """验收脚本必须是合法 bash，且真的检查三件事。

    ①有容器在 Restarting/Exited/unhealthy →立刻失败（不等满90秒）；
    ②目标服务必须都在 running；
    ③启动日志里不能有 traceback（容器 restart后可能短暂显示 Up，
    只看状态会被骗）。
    """
    sh = tmp_path / "v.sh"
    sh.write_text(deploy.verify_script("api jobs agent",90))
    out = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
    assert out.returncode ==0, f"验收脚本不是合法 bash：{out.stderr}"

    body = sh.read_text()
    assert "Restarting" in body, "没检查重启循环"
    assert "Traceback (most recent call last)" in body, "没扫启动日志的 traceback"
    assert "stock-screener-jobs-1" in body, "没检查 jobs容器"
    assert "SS_VERIFY=OK" in body, "没有成功标记——调用方无法判定"


def test_only_explicit_ok_counts_as_success(monkeypatch):
    """**只有明确见到 SS_VERIFY=OK才算成功**。

    ssh失败、脚本报错、输出被截断——都不能被当成部署成功。
    """
    cases = [
        ("SS_VERIFY=OK", "OK"),
        ("SS_VERIFY=FAIL:容器异常 x", "容器异常 x"),
        ("", None),
        ("docker: command not found", None),
    ]
    for out, want in cases:
        monkeypatch.setattr(deploy, "_capture", lambda cmd, o=out: o)
        got = deploy.verify_deployment("h", "api jobs agent",1)
        if want is None:
            assert got != "OK", f"输出 {out!r}不该被判成功"
        else:
            assert got == want, f"输出 {out!r}期望 {want!r}，实际 {got!r}"


def test_rollback_restores_the_previous_image(monkeypatch):
    """回滚必须重新打 tag并重建容器，而不是只打印一句话。"""
    seen = []
    def cap(cmd):
        seen.append(cmd)
        return "SS_ROLLBACK=OK"
    monkeypatch.setattr(deploy, "_capture", cap)

    assert deploy.rollback("h", "/srv/app", "api jobs agent") is True
    joined = " ".join(seen[0])
    assert "stock-screener:rollback" in joined, "没从 rollback标签恢复"
    assert "docker tag" in joined, "没重新打 latest标签"
    assert "up -d --force-recreate" in joined, "没重建容器"


def test_main_returns_nonzero_when_verification_fails(monkeypatch):
    """验收失败必须返回非零、**尝试过回滚**，且快照发生在构建之前。

    v0.2.114的真事：deploy.py退出0、打印「请自行核对」，
    于是 jobs重启循环没人知道。
    """
    calls = _patch_common(monkeypatch, verify="jobs启动异常", rollback_ok=True)

    code = deploy.main(["--host", "h", "--dir", "/srv/app"])
    assert code !=0, "验收失败却返回成功"
    assert "rollback" in calls, "验收失败但没有回滚"
    snap_i = calls.index("snapshot")
    build_i = next(i for i, c in enumerate(calls) if "build api" in c)
    assert snap_i < build_i, ("快照必须在构建之前——否则新构建覆盖 latest，"
    "回滚就没有镜像可用")


def test_main_reports_success_only_after_verification(monkeypatch, capsys):
    """验收通过才打印成功。"""
    calls = _patch_common(monkeypatch, verify="OK")
    code = deploy.main(["--host", "h", "--dir", "/srv/app"])
    out = capsys.readouterr().out
    assert code ==0
    assert "验收通过" in out
    assert "rollback" not in calls, "验收通过却触发了回滚"


def test_verify_script_wait_is_bounded(monkeypatch):
    """等待时间必须由参数决定，不能无限等（CI里会挂死）。"""
    sh = deploy.verify_script("api",30)
    assert "30" in sh
    sh2 = deploy.verify_script("api",5)
    assert "5" in sh2 and sh != sh2
