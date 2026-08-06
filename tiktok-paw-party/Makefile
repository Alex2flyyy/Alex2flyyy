.PHONY: help install dev test test-fast lint ideas run preview clean doctor channels stats costs

PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install PawParty
	./scripts/install.sh

dev:  ## Install with test dependencies
	./scripts/install.sh --dev

doctor:  ## Environment and config health check
	$(BIN)/pawparty doctor

channels:  ## List channels and their creative space
	$(BIN)/pawparty channels

test:  ## Run the full suite (includes real ffmpeg renders, ~5 min)
	$(BIN)/pytest -q

test-fast:  ## Run everything except the ffmpeg render tests
	$(BIN)/pytest -q -m "not ffmpeg"

ideas:  ## Generate 7 concepts — free, no rendering
	$(BIN)/pawparty ideas --count 7

preview:  ## Render 2 videos to check the pipeline end to end
	$(BIN)/pawparty run --count 2

run:  ## The daily batch: 7 videos, scheduled, packaged
	$(BIN)/pawparty run --count 7 --schedule --export

stats:  ## Novelty ledger statistics
	$(BIN)/pawparty stats

costs:  ## Provider spend report
	$(BIN)/pawparty costs

clean:  ## Delete intermediates older than 14 days (keeps Finished/)
	$(BIN)/pawparty clean --keep-days 14
