import yaml

REQUIRED = {"api", "jobs", "agent", "ntfy", "cloudflared"}
MEM = {"api": "512m", "jobs": "1g", "agent": "512m", "ntfy": "64m", "cloudflared": "64m"}


def test_compose_rules():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    assert c["name"] == "stock-screener" and REQUIRED <= set(c["services"])
    for name, svc in c["services"].items():
        assert "ports" not in svc, name
        env = svc.get("environment", {})
        assert all(env.get(k) == "" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")), name
        if name in MEM: assert svc["mem_limit"] == MEM[name], name


def test_env_files_inject_notify_env():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name in [n for n in ("api", "jobs", "agent") if n in c["services"]]:   # jobs/agent 到 Task 28 才入列
        ef = c["services"][name]["env_file"]
        assert any("llm.env" in e for e in ef) and any("notify.env" in e for e in ef), name
        assert not any("westock.env" in e for e in ef), name     # westock 走挂载，不走 env_file


def test_backfill_is_tools_only_and_one_shot():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    bf = c["services"]["backfill"]
    assert bf.get("profiles") == ["tools"]
    assert "restart" not in bf                    # 一次性任务：跑完即退，restart 会无限循环回填
    assert "build" not in bf                      # 复用 prod 镜像
    assert bf["image"] == "stock-screener:${TAG:-latest}"
    assert any("state" in v for v in bf["volumes"])


def test_state_mount_is_subdir_not_whole_config():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name in ("api", "jobs", "agent"):
        mounts = [v for v in c["services"][name]["volumes"] if "/state" in v]
        assert mounts and ".config/stock-screener/state:" in mounts[0], name
        # 整目录挂载会让容器读写 llm.env / notify.env 密钥
        assert not any(v.endswith("stock-screener:/state:rw") for v in c["services"][name]["volumes"]), name


def test_jobs_has_healthcheck_with_mtime():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    hc = c["services"]["jobs"]["healthcheck"]
    assert any("mmin -2" in str(x) for x in hc["test"])
    assert str(hc["interval"]) == "60s" and str(hc["start_period"]) == "300s"


def test_non_root_users():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name in ("api", "jobs", "agent", "backfill", "ntfy", "cloudflared"):
        assert c["services"][name].get("user") == "${UID:-1000}:${GID:-1000}", name


def test_jobs_healthcheck_is_not_always_true():
    """`test: true` 之类的恒真命令必须被抓出来：只看「含 mmin -2 字样」挡不住。"""
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    cmd = " ".join(str(x) for x in c["services"]["jobs"]["healthcheck"]["test"])
    assert cmd.startswith("CMD-SHELL")
    assert "find /tmp/jobs_healthy -mmin -2" in cmd
    assert "|| exit 1" in cmd
    assert not cmd.strip().endswith("; true")


def test_no_deploy_resources_limits():
    """内存上限只能写 mem_limit：deploy.resources 在非 swarm 下被静默忽略。"""
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name, svc in c["services"].items():
        assert "deploy" not in svc, name


def test_data_and_config_paths_are_repo_root_relative():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name in ("api", "jobs", "agent"):
        mounts = c["services"][name]["volumes"]
        assert any(v.startswith("./data:") for v in mounts), name
        assert any(v.startswith("./config:") for v in mounts), name
        assert not any("deploy/data" in v for v in mounts), name


def test_shared_image_no_duplicate_build():
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name in ("api", "jobs", "agent"):
        assert c["services"][name]["image"] == "stock-screener:${TAG:-latest}", name
    assert "build" in c["services"]["api"]
    assert "build" not in c["services"]["jobs"] and "build" not in c["services"]["agent"]


def test_every_service_has_bounded_log_rotation():
    """每个服务都必须有日志轮转，且配置写在仓库里。

    原先轮转只存在于 VM 的 `/etc/docker/daemon.json`——那是**机器状态、不进版本控制**。
    换机器或重装 Docker 就丢，届时 json-file 默认**无上限**，日志会一直涨到写满磁盘，
    而且不报错。（实测用量约 500KB/天：jobs 783B、api 78KB 且 99.6% 是 /healthz、
    ntfy 407KB；有上限时单容器最多 150MB。）
    """
    c = yaml.safe_load(open("deploy/docker-compose.yml"))
    for name, svc in c["services"].items():
        lg = svc.get("logging")
        assert lg is not None, (
            f"{name} 没有 logging 配置：轮转只写在 VM 的 daemon.json 里，"
            "换机器就丢，日志会无上限增长")
        assert lg["driver"] == "json-file", name
        assert lg["options"]["max-size"], f"{name} 没设 max-size"
        # max-file 不设时默认 1，等于只有一份、轮转形同虚设
        assert int(lg["options"]["max-file"]) >= 2, f"{name} 的 max-file 太小：轮转会失效"


def test_log_rotation_is_shared_via_anchor():
    """轮转用 YAML 锚点共用，避免六个服务各写一遍、改一处漏五处。"""
    text = open("deploy/docker-compose.yml", encoding="utf-8").read()
    assert "&default-logging" in text and "*default-logging" in text, \
        "没有用锚点共用日志配置；六个服务各写一份迟早改漏"


def test_dockerignore_excludes_local_review_files():
    """`.tmp_smoke/` / `.firecrawl/` 绝不能进镜像构建上下文。

    `deploy/Dockerfile` 是 `COPY . /app`——凡 `.gitignore` 忽略但 `.dockerignore`
    没排除的文件，都会被打进**生产镜像**。Codex round18 发现审查 spec
    （`.tmp_smoke/codex-round18-spec.md`）里含真实域名、服务器 IP 与用户名，
    却只被 `.gitignore` 挡着，本地 `docker compose build` 会把它拷进镜像。
    """
    text = open(".dockerignore", encoding="utf-8").read()
    for path in (".tmp_smoke", ".firecrawl"):
        assert path in text, (
            f".dockerignore 没排除 {path}：COPY . /app 会把它送进镜像构建上下文")


def test_dockerignore_covers_all_env_variants():
    """`.env.local` / `.env.production` 这类名字也要挡住。"""
    text = open(".dockerignore", encoding="utf-8").read()
    assert ".env.*" in text, ".dockerignore 只挡了 *.env，漏掉 .env.local 等变体"
