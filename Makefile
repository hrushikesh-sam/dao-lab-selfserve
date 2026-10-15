-include .env
export
DAOLAB_API_HOST ?= 0.0.0.0
DAOLAB_API_PORT ?= 8080

.PHONY: setup test api engine engine-loop dev
setup:
	python -m pip install -e .
test:
	python -m pytest tests/ -q
api:
	python -m uvicorn service.api:app --host $(DAOLAB_API_HOST) --port $(DAOLAB_API_PORT)
# `engine`/`dev` boot the full local stack (API + engine) via scripts/run/dev.sh so one
# command starts both and Ctrl-C stops both (plan/10 B-30). `engine-loop` runs the engine
# alone (the systemd-timer workflow in plan/04).
engine dev:
	scripts/run/dev.sh
engine-loop:
	python -m service.engine --loop
