import shutil
import subprocess
from pathlib import Path

import pytest


def test_compose_rejects_missing_env_files(tmp_path):
    """compose 解析项目时就会检查 env_file 是否存在——哪怕只启动 ntfy。

    RUNBOOK 第 1 节必须先建出三份 env 文件，否则第 4 节的 `up -d ntfy` 直接失败，
    而且报错指向 api 的 llm.env 而不是 ntfy，排查方向会被带偏。

    不改 HOME（改 HOME 会让 docker 找不到 compose 插件，测的就不是这件事了）：
    把 compose 里的 ${HOME} 换成一个空临时目录再解析。
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("容器内没有 docker（make test 在 dev 镜像里跑）；本用例在宿主机上跑才有意义")
    if subprocess.run([docker, "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("本机的 docker compose 不可用")

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    patched = Path("deploy/docker-compose.yml").read_text(encoding="utf-8").replace("${HOME}", str(empty_home))
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(patched, encoding="utf-8")

    r = subprocess.run(
        [docker, "compose", "-f", str(compose_file), "--project-directory", ".", "config", "-q"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0, "env_file 缺失时 compose 竟然解析通过——RUNBOOK 的顺序约束需要重写"
    assert "llm.env" in (r.stderr + r.stdout)


def test_placeholder_host_is_detected():
    """占位域名必须能被识别出来，供启动时提示用户。

    `config/notify.yaml` 的 `public_host` 出厂值是 `stock.example.com`。它不是真实域名：
    同源校验（`Origin == https://{public_host}`）会拒绝所有真实请求，
    而健康检查、盘后选池都照常绿——**用户只会看到「网页按钮点了没反应」**。
    """
    from core.config import is_placeholder_host

    assert is_placeholder_host("stock.example.com") is True
    assert is_placeholder_host("example.com") is True
    assert is_placeholder_host("stock.example.org") is True
    assert is_placeholder_host("stock.my-domain.cn") is False
    assert is_placeholder_host("localhost") is False
    assert is_placeholder_host("") is False
    assert is_placeholder_host(None) is False


def test_startup_warns_on_placeholder_host():
    """启动路径必须真的**调用**这条检查——定义而不调用等于没有。

    旧版对 api 只断言 `"is_placeholder_host" in api_src`，而函数体里本来就有这个词，
    于是 api 侧的检查从未被调用、守卫却全绿。
    """
    import ast
    from pathlib import Path

    def calls_in(path: str, fn: str) -> list[int]:
        src = Path(path).read_text(encoding="utf-8")
        return [n.lineno for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == fn]

    assert calls_in("jobs/main.py", "_warn_if_placeholder_config"), \
        "jobs 启动没调用占位域名检查"
    assert calls_in("api/main.py", "_warn_if_placeholder_config"), \
        "api 启动没调用占位域名检查（定义了但没调用）"

    # 还必须在 `app = create_app(...)` **之前**：装配之后再提示，用户已经看到失效页面了
    api_src = Path("api/main.py").read_text(encoding="utf-8")
    api_calls = calls_in("api/main.py", "_warn_if_placeholder_config")
    assert api_calls[0] < api_src.index("app = create_app("), \
        "api 的占位检查在 app 装配之后调用"


def test_web_password_check_is_only_on_api():
    """口令检查必须在 api 侧，**不能在 jobs 侧**。

    `deploy/docker-compose.yml` 只把 `web.env` 挂给 `api`（agent/jobs 不对外）。
    jobs 进程里永远读不到 `SS_WEB_PASS`，在那儿检查会产生一条**恒真**的误报告警：
    每次启动都喊「网页不鉴权」，而实际上 api 侧配得好好的。
    （2026-11-13 上线后实机日志确实出现了这条假警报。）
    """
    import ast
    from pathlib import Path

    def env_reads(path: str, key: str) -> list[int]:
        """找出**真正读**环境变量 `key` 的代码行号（注释与 docstring 里的提到不算）。

        同时覆盖三种写法，别让 `import os as _os` 之类的别名绕过：
          - `os.environ.get("KEY")`
          - `os.environ["KEY"]`
          - `getenv("KEY")`
        """
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if key not in args:
                continue
            attr_chain = []
            cur = f
            while True:
                if isinstance(cur, ast.Attribute):
                    attr_chain.append(cur.attr)
                    cur = cur.value
                elif isinstance(cur, ast.Subscript):
                    attr_chain.append("<subscript>")
                    cur = cur.value
                else:
                    break
            # `.environ` 出现在链上，或直接是 getenv(...)
            if "environ" in attr_chain or getattr(f, "attr", None) == "getenv":
                found.append(node.lineno)
        return found

    assert env_reads("jobs/main.py", "SS_WEB_PASS") == [], \
        "jobs 侧真的在读 SS_WEB_PASS：它读不到 web.env，这会产生恒真误报"
    assert env_reads("api/main.py", "SS_WEB_PASS"), \
        "api 侧没有检查网页口令"
