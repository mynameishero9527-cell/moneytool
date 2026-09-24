.PHONY: check lint format typecheck test test-network frontend build e2e bench dev

PY ?= python

check: lint typecheck test

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy moneytool

test:
	pytest -m "not network"

test-network:
	pytest -m network

frontend:
	$(PY) scripts/build_frontend.py

build: frontend
	$(PY) -m build --wheel

bench:
	$(PY) scripts/bench.py

dev:
	$(PY) -m moneytool run --no-browser
