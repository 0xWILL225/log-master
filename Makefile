# log-master developer and deployment tasks. Run `make help` for an overview.

PYTHON_ENV := env -u PYTHONPATH
PREFIX ?= /opt/log-master
CONFIG ?= /etc/log-master/config.yaml
SERVICE_USER ?= log-master
# Interpreter for the service venv. Must be >= 3.11 and readable by SERVICE_USER (not under a home directory).
PYTHON ?= /usr/bin/python3
UNIT_DIR ?= /etc/systemd/system

.PHONY: help sync frontend build spec check-spec lint format test test-e2e dev install

help:
	@echo "sync        install the Python workspace (uv) and frontend dependencies (npm)"
	@echo "frontend    build the SPA into packages/log_master/src/log_master/static/"
	@echo "build       frontend + wheels for log-master and logserver into dist/"
	@echo "spec        regenerate spec/*.openapi.yaml and the frontend API types"
	@echo "check-spec  fail if the committed OpenAPI documents or frontend types are stale"
	@echo "lint        ruff + svelte-check"
	@echo "test        Python unit and integration tests"
	@echo "test-e2e    Playwright smoke tests (needs 'make frontend'; PLAYWRIGHT_CHANNEL=chrome for system Chrome)"
	@echo "dev         run two fake servers and log-master on :8080 (use 'npm run dev' in frontend/ for hot reload)"
	@echo "install     (root) install to PREFIX=$(PREFIX) with a systemd unit; see README"

sync:
	uv sync
	cd frontend && npm ci

frontend:
	cd frontend && npm ci && npm run build

build: frontend
	uv build --package logserver --out-dir dist
	uv build --package log-master --out-dir dist

spec:
	$(PYTHON_ENV) uv run python scripts/gen_openapi.py
	cd frontend && npm run gen:api

check-spec:
	$(PYTHON_ENV) uv run python scripts/gen_openapi.py --check
	cd frontend && npx openapi-typescript ../spec/log-master.openapi.yaml -o /tmp/log-master-schema.d.ts \
		&& diff -q /tmp/log-master-schema.d.ts src/lib/api/schema.d.ts

lint:
	uv run ruff check packages tests scripts
	uv run ruff format --check packages tests scripts
	cd frontend && npm run check

format:
	uv run ruff format packages tests scripts
	uv run ruff check --fix packages tests scripts

test:
	$(PYTHON_ENV) uv run pytest

test-e2e:
	cd frontend && npx playwright test

dev:
	$(PYTHON_ENV) uv run python scripts/dev_stack.py

install:
	@test -n "$$(ls dist/log_master-*.whl 2>/dev/null)" || (echo "run 'make build' first" && exit 1)
	id -u $(SERVICE_USER) >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin $(SERVICE_USER)
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
		|| (echo "$(PYTHON) is older than Python 3.11; pass PYTHON=/path/to/python3.11+" && exit 1)
	uv venv --allow-existing --python $(PYTHON) $(PREFIX)
	uv pip install --python $(PREFIX)/bin/python --link-mode copy --reinstall dist/logserver-*.whl dist/log_master-*.whl
	install -d -o $(SERVICE_USER) -g $(SERVICE_USER) $(dir $(CONFIG))
	test -e $(CONFIG) || install -m 644 -o $(SERVICE_USER) -g $(SERVICE_USER) config.example.yaml $(CONFIG)
	sed -e 's|@PREFIX@|$(PREFIX)|g' -e 's|@CONFIG@|$(CONFIG)|g' -e 's|@SERVICE_USER@|$(SERVICE_USER)|g' \
		system/log-master.service.in > $(UNIT_DIR)/log-master.service
	ln -sf $(PREFIX)/bin/log-master /usr/local/bin/log-master
	systemctl daemon-reload
	@echo "installed; enable with: systemctl enable --now log-master"
