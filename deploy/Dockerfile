# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# westock 除权事件走 Node CLI。只拷二进制不够：npm 的 bin 是依赖相对路径的 shim，
# 必须同时拷 npm 目录并补符号链接，否则 npm ci 报 Cannot find module '../lib/cli.js'。
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm --version && npx --version

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY package.json package-lock.json ./
RUN npm ci

# web/dist 必须在镜像里自建：宿主机产物被 .dockerignore 排除，依赖宿主机构建会让
# prod 的 COPY 拿到空目录。
FROM node:20-bookworm-slim AS webuild
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM base AS dev
# **dev 镜像必须带 git**：`tests/test_version.py`、`test_release_script.py`、
# `test_scan_secrets.py` 都要读 git 历史/tag。缺 git 时这些测试不是跳过而是
# 报 `env: 'git': Permission denied` 之类的假红（2026-11-21 在 81.4 上实测）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

FROM base AS prod
COPY . /app
COPY --from=webuild /web/dist /app/web/dist
