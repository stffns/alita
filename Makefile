ROOT := $(shell pwd)
VENV := $(ROOT)/.venv
PY   := $(VENV)/bin/python
PYTHONPATH := $(ROOT):$(ROOT)/../vstash-local
export PYTHONPATH

LAUNCH_DIR := $(HOME)/Library/LaunchAgents
TG_LABEL   := com.pelops.telegram
SC_LABEL   := com.pelops.scheduler
LOG_FILE   := $(ROOT)/data/logs/pelops.log

.DEFAULT_GOAL := help

help:  ## show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- run (foreground) ----

bot: $(PY)  ## run telegram bot in foreground
	$(PY) -m pelops.telegram_bot

scheduler: $(PY)  ## run standalone scheduler (legacy; bot embeds one)
	$(PY) -m pelops.scheduler

ui: $(PY)  ## run chainlit web UI
	$(VENV)/bin/chainlit run pelops/ui.py -w

doctor: $(PY)  ## run pelops-doctor health checks
	$(PY) -m pelops.doctor

# ---- launchd (autostart) ----

install:  ## install launchd agents (telegram + scheduler, autostart at login)
	bash scripts/install-launchd.sh

uninstall:  ## remove launchd agents
	bash scripts/install-launchd.sh --uninstall

status:  ## show launchd status for pelops agents
	@launchctl list | grep com.pelops || echo "no pelops agents loaded"

restart:  ## restart the telegram launchd agent
	launchctl kickstart -k gui/$(shell id -u)/$(TG_LABEL)

# ---- logs ----

logs:  ## tail pelops.log
	tail -F $(LOG_FILE)

errlogs:  ## grep ERROR lines from pelops.log
	@grep -E '"level": "ERROR"' $(LOG_FILE) | tail -20

# ---- dev ----

test: $(PY)  ## run pytest
	$(PY) -m pytest -q

lint: $(PY)  ## ruff check + format check
	$(VENV)/bin/ruff check pelops tests
	$(VENV)/bin/ruff format --check pelops tests

fmt: $(PY)  ## ruff format (in place)
	$(VENV)/bin/ruff format pelops tests

fix-pth:  ## unhide .pth files macOS keeps re-hiding inside .venv
	bash scripts/fix-pth.sh

# ---- guards ----

$(PY):
	@echo "error: $(PY) is missing. Run 'uv venv && uv pip install -e .' first." >&2
	@exit 1

.PHONY: help bot scheduler ui doctor install uninstall status restart logs errlogs test lint fmt fix-pth
