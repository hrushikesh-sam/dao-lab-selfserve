-include .env
export
DAOLAB_API_HOST ?= 0.0.0.0
DAOLAB_API_PORT ?= 8080

.PHONY: setup test api engine
setup:
	python -m pip install -e .
test:
	python -m pytest tests/ -q
api:
	python -m uvicorn service.api:app --host $(DAOLAB_API_HOST) --port $(DAOLAB_API_PORT)
engine:
	python -m service.engine --loop
