# 跨平台：优先用仓库里的 venv（Windows 是 .venv/Scripts，unix 系是 .venv/bin），
# 两者都没有才退回系统 python。别把 Windows 路径写死，否则 clone 到 macOS/Linux 直接跑不了。
VENV_PY_WIN := .venv/Scripts/python.exe
VENV_PY_POSIX := .venv/bin/python
PY ?= $(if $(wildcard $(VENV_PY_WIN)),$(VENV_PY_WIN),$(if $(wildcard $(VENV_PY_POSIX)),$(VENV_PY_POSIX),python))
PIP ?= $(PY) -m pip

.PHONY: help install check-config check-assets normalize-assets check-cleanroom serve test run-app clean

help:
	@echo "install       创建 venv 并以可编辑模式安装（dev 依赖含 PySide6，装完能直接跑界面）"
	@echo "check-config  校验外壳配置（data/settings/app.json）与模型"
	@echo "check-assets  体检 assets/（alpha / 分辨率 / 编码）"
	@echo "normalize-assets  归一化姿态图 → assets/透明_normalized/（统一画布）"
	@echo "check-cleanroom  干净房间体检：只放会入库的文件，验证 clone 下来能跑"
	@echo "serve         启动引擎常驻服务（IPC 服务端，等前端连入）"
	@echo "test          pytest"
	@echo "run-app       启动 Qt 前端（等价于装好后的 noesia-gui）"
	@echo "clean         清理缓存与测试产物"

install:
	python -m venv .venv
	@if [ -x "$(VENV_PY_POSIX)" ]; then $(VENV_PY_POSIX) -m pip install -e ".[dev]"; \
	 else $(VENV_PY_WIN) -m pip install -e ".[dev]"; fi

check-config:
	$(PY) -m noesia_entry check-config

check-assets:
	$(PY) -m noesia_entry check-assets

normalize-assets:
	$(PY) scripts/normalize_assets.py --write

check-cleanroom:
	$(PY) scripts/check_cleanroom.py

serve:
	$(PY) -m noesia_entry serve

test:
	$(PY) -m pytest

run-app:
	cd src && $(abspath $(PY)) -m app

clean:
	rm -rf .pytest_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
