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


def _compose():
    import yaml

    return yaml.safe_load(open("deploy/docker-compose.yml"))


def test_every_service_caps_cpu():
    """**每个服务都必须有 `cpus` 上限。**

    这台机器是共享的（4 核，还跑着 WeKnora/selfhost/langbot，别人也在编译）。
    不加限制时一次 `compose build`（pip 装 pandas/numpy/TA-Lib + 两次 npm ci +
    vite build）就能跑满全部核心，ssh 卡到 banner 交换超时、healthz 直接 530
    ——2026-11-24 实测过。

    `mem_limit` 只管内存，完全不管 CPU，两者不能互相替代。
    """
    bad = []
    for name, svc in _compose()["services"].items():
        cpus = svc.get("cpus") or svc.get("cpu_quota") or svc.get("cpu_shares")
        if not cpus:
            bad.append(name)
    assert not bad, f"这些服务没有 CPU 上限，会抢爆共享机：{bad}"


def test_every_cpu_limit_is_a_sane_fraction():
    """CPU 上限必须是正数且不超过机器总核数。

    写成 0 或负数会被 docker 拒绝启动；写成 100 等于没限。
    """
    for name, svc in _compose()["services"].items():
        cpus = svc.get("cpus")
        if cpus is None:
            continue
        assert isinstance(cpus, (int, float)), f"{name}: cpus 不是数字：{cpus!r}"
        assert 0 < float(cpus) <= 8, f"{name}: cpus={cpus} 不合理（应 0<×≤8）"


def test_jobs_has_grace_period_longer_than_its_longest_task():
    """**jobs 的优雅停机必须长于它最长的任务**，否则 SIGKILL 会毁数据。

    EOD 事务约 3 分钟；备份要拷 1.3GB（实测 8 分钟，含 integrity_check）。
    compose 默认 10 秒——2026-11-24 就是这样把当晚备份弄丢的
    （`.market-20261124.db.tmp` 残留，备份永久缺失）。
    """
    svc = _compose()["services"]["jobs"]
    grace = svc.get("stop_grace_period")
    assert grace, "jobs 没有 stop_grace_period，默认 10s 会被 SIGKILL"
    if isinstance(grace, (int, float)):
        secs = float(grace)
    else:
        t = str(grace).strip().lower()
        if t.endswith("s"):
            secs = float(t[:-1])
        elif t.endswith("m"):
            secs = float(t[:-1]) * 60
        else:
            secs = float(t)
    assert secs >= 120, f"jobs 的 stop_grace_period={grace}，EOD/备份会被强杀"


def test_cloudflared_has_a_healthcheck_that_detects_a_dead_tunnel():
    """**隧道卡死必须能被看见。**

    2026-11-26 真实故障：`cloudflared` Up 41 小时后 4 条 QUIC 连接全断，
    之后一直重试但再也没连上，公网整站 1033；而 `docker ps` 显示 `Up`，
    因为进程没退出、`restart: unless-stopped` 不会触发。
    没有健康检查时，唯一的信号是"用户发现网站打不开"。
    """
    svc = _compose()["services"]["cloudflared"]
    hc = svc.get("healthcheck")
    assert hc, "cloudflared 没有 healthcheck，隧道卡死时状态仍然是 Up"
    test = hc["test"]
    joined = " ".join(test) if isinstance(test, list) else str(test)
    # 必须真的问"隧道连上了吗"，而不是只判进程活着
    assert "ready" in joined, f"健康检查没判隧道就绪状态：{joined}"
    # metrics 端点得开，否则 ready 探针无处可问
    cmd = svc.get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "--metrics" in cmd, f"没开 metrics 端点，/ready 探不到：{cmd}"
