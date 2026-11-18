"""真执行 `remote_extract_script` 生成的 shell，验证部署语义。

**为什么必须真跑**：这套逻辑连着踩了三次坑，每次都有"看起来对"的字符串守卫放行：
  1. `--exclude` —— 首次部署时配置一份都不写入
  2. `--skip-old-files` —— 代码更新全被跳过，部署变成空操作
  3. `for p in "a", "b"` —— shell 里逗号是字面字符，第一个路径带逗号、`test -f`
     判否 → 备份被静默跳过 → 生产配置被 `stock.example.com` 覆盖，
     站点所有写操作开始 400（实机事故）

三者的共同点：语法都合法、没有报错，只有**真跑一次看结果**才能发现。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("tar") is None, reason="需要 tar")


def _script(sha: str, target: Path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib

    import deploy
    importlib.reload(deploy)
    return deploy.remote_extract_script(sha, str(target))


def _make_archive(tmp: Path, files: dict[str, str]) -> Path:
    src = tmp / "src"
    for rel, content in files.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    archive = tmp / f"src-{tmp.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for item in src.rglob("*"):
            tf.add(item, arcname=str(item.relative_to(src)))
    return archive


def _run(tmp: Path, sha: str, target: Path) -> subprocess.CompletedProcess:
    """脚本读的是 `/tmp/src-{sha}.tar.gz`，把归档放到那个绝对路径去。"""
    script = _script(sha, target)
    remote = Path(f"/tmp/src-{sha}.tar.gz")
    shutil.copyfile(tmp / f"src-{sha}.tar.gz", remote)
    try:
        return subprocess.run(["sh", "-c", script], cwd=str(target),
                              capture_output=True, text=True)
    finally:
        remote.unlink(missing_ok=True)


def test_first_deploy_writes_configs(tmp_path):
    """目标为空：配置模板必须正常写入，否则容器 ConfigError 起不来。"""
    target = tmp_path / "dst"
    target.mkdir()
    _make_archive(tmp_path, {"config/notify.yaml": "template\n",
                             "deploy/ntfy/server.yml": "template-ntfy\n",
                             "config/llm.yaml": "template-llm\n",
                             "jobs/main.py": "code\n"})
    sha = tmp_path.name
    r = _run(tmp_path, sha, target)
    assert r.returncode == 0, r.stderr
    assert (target / "config" / "notify.yaml").read_text(encoding="utf-8") == "template\n", \
        "首次部署没有写入 config/notify.yaml"
    assert (target / "deploy" / "ntfy" / "server.yml").exists()
    assert (target / "config" / "llm.yaml").read_text(encoding="utf-8") == "template-llm\n"
    assert (target / "jobs" / "main.py").exists()


def test_redeploy_updates_code_but_keeps_config(tmp_path):
    """再次部署：代码必须更新，配置必须原样保留。

    这正是事故场景——配置被覆盖成 `stock.example.com` 后，同源校验
    （`Origin == https://{public_host}`）拒绝了所有写操作，而健康检查照常 200。
    """
    target = tmp_path / "dst"
    (target / "config").mkdir(parents=True)
    (target / "deploy" / "ntfy").mkdir(parents=True)
    (target / "jobs").mkdir(parents=True)
    (target / "config" / "notify.yaml").write_text("real-domain\n", encoding="utf-8")
    (target / "config" / "llm.yaml").write_text("real-llm\n", encoding="utf-8")
    (target / "deploy" / "ntfy" / "server.yml").write_text("real-ntfy\n", encoding="utf-8")
    (target / "jobs" / "main.py").write_text("OLD CODE\n", encoding="utf-8")

    _make_archive(tmp_path, {"config/notify.yaml": "template\n",
                             "deploy/ntfy/server.yml": "template-ntfy\n",
                             "config/llm.yaml": "template-llm\n",
                             "jobs/main.py": "NEW CODE\n"})
    sha = tmp_path.name
    r = _run(tmp_path, sha, target)
    assert r.returncode == 0, r.stderr

    assert (target / "jobs" / "main.py").read_text(encoding="utf-8") == "NEW CODE\n", \
        "代码没有更新：部署是空操作"
    assert (target / "config" / "notify.yaml").read_text(encoding="utf-8") == "real-domain\n", \
        "生产配置被覆盖成模板值（会导致同源校验拒绝所有写操作）"
    assert (target / "deploy" / "ntfy" / "server.yml").read_text(encoding="utf-8") == "real-ntfy\n", \
        "ntfy 配置被覆盖"
    assert (target / "config" / "llm.yaml").read_text(encoding="utf-8") == "real-llm\n", \
        "LLM 端点配置被覆盖"


def test_script_has_no_comma_list_for_loop():
    """反向守卫：不能再用 `for p in "a", "b"` —— 逗号在 shell 里是字面字符。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import deploy
    script = deploy.remote_extract_script("abc", "/srv/app")
    assert 'for p in' not in script, "又在用 for 列表遍历；逗号会被当成文件名的一部分"
    # 每个需要保护的文件都要有独立的备份与恢复两条命令
    for rel in deploy.PRESERVE_ON_TARGET:
        assert f'cp -p "{rel}" "$bak/{rel}"' in script, f"{rel} 没有备份"
        assert f'cp -p "$bak/{rel}" "{rel}"' in script, f"{rel} 没有恢复"


def test_agent_yaml_is_preserved_on_target():
    """`agent.yaml` 必须在保留名单里，否则升级会把站点配置覆盖成仓库值。

    真实事故（v0.2.71→v0.2.72）：`llm.yaml` 保留、`agent.yaml` 不保留，
    升级后**正式模式打开、预算还是旧的 300000**，上线当天 10:42 就打满。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import deploy
    assert "config/agent.yaml" in deploy.PRESERVE_ON_TARGET, (
        "agent.yaml 不在保留名单：升级会覆盖站点的 shadow_mode 等设置")


def test_script_warns_when_preserved_config_lacks_expected_keys():
    """保留配置缺失关键键时要提示（沉默会让人以为新默认值已生效）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import deploy
    script = deploy.config_drift_script("/srv/app")
    assert "config/agent.yaml" in script and "shadow_mode" in script
    assert "config/llm.yaml" in script and "agent:" in script
