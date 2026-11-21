IMAGE ?= ss-dev
# 每次用唯一网络名（$$$$ 交给 shell 展开成 PID）：固定名字在并发跑或异常退出时，
# 结尾的 network rm 可能删掉另一个会话正在用的网络。
NET ?= ss-test-$$$$

.PHONY: test test-web test-integration release check-version

# 测试一律在 dev 镜像里跑：生产是 3.12 + TA-Lib，宿主机 Python 不参与。
# 用 $$PWD 而不是 $PWD —— 后者会被 make 展开成 $P + 字面 WD，挂载成空卷。
# **`--tmpfs /tmp` 是 2026-11-21 事故的防线**：长期测试容器的 /tmp 里堆了
# 33 万个变更路径、可写层涨到 230 GB，撑爆 Docker 虚拟磁盘。
# 挂成 tmpfs 后，测试写的任何临时文件**随容器销毁而消失**，且容量被
# `size=` 封顶——再怎么写也撑不爆宿主机磁盘。
# `--rm` 保证容器不残留（长期 `sleep infinity` 的守护容器不享受这个清理）。
test:
	docker build --target dev -t $(IMAGE) -f deploy/Dockerfile .
	docker run --rm --tmpfs /tmp:rw,size=2g -v "$$PWD":/app -w /app $(IMAGE) pytest -q

# 测试进程本身也在容器里，必须与 ntfy 同网络才解析得到容器名；不发布任何端口。
test-integration:
	docker build --target dev -t $(IMAGE) -f deploy/Dockerfile .
	-docker network create $(NET)
	docker run -d --rm --name ss-ntfy --network $(NET) binwiederhier/ntfy serve --listen-http :2586
	docker run --rm --network $(NET) -e NTFY_TEST_URL=http://ss-ntfy:2586 -v "$$PWD":/app -w /app $(IMAGE) pytest -q -m integration; status=$$?; docker stop ss-ntfy; docker network rm $(NET); exit $$status

# 前端镜像可覆盖：与 deploy/Dockerfile 的 web 构建阶段保持一致才最可靠，
# 但某个环境本地没这个 tag 时，用 WEB_NODE_IMAGE=node:22-slim 也能跑。
WEB_NODE_IMAGE ?= node:20-bookworm-slim

# 前端测试：45 个用例（路由、聚合、涨跌色、日志页、个股页）。
# **它一直没进门禁**：`make test` 只跑 pytest，所以「前端测试全绿」这句话
# 从来没有依据。npm ci 走容器缓存卷，避免每次重装。
test-web:
	docker run --rm -v "$$PWD/web":/w -w /w -v ss-npm-cache:/npm-cache \
	  -e npm_config_cache=/npm-cache $(WEB_NODE_IMAGE) \
	  sh -c "npm ci --silent && npx vitest run"

# 版本门禁：包清单 + core/version.py + CHANGELOG 小节 + git tag 必须一致。
# **不依赖 Docker**：纯文本检查没必要先 build 镜像（Docker Hub 一抽风就跑不了），
# 而且它要在「改完版本、跑全量测试之前」尽快给出反馈。
check-version:
	python3 -m pytest -q tests/test_version.py

# 发布：make release VERSION=0.2.1
# 默认干跑；只有 APPLY=1 才真执行。
# **不能用 $(if $(APPLY),...)**：make 把任何非空串当真值，APPLY=0 / APPLY=false
# 都会展开成 --apply 而触发真实发布（复核实测过）。
release:
	python3 scripts/release.py $(VERSION) $(if $(filter 1,$(APPLY)),--apply,)
