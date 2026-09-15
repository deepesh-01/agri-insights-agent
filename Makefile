# Conversational text-to-SQL agent -- developer entry points.
#
# Native, Metal-accelerated. This project ships no Docker setup: on Apple
# Silicon a container cannot reach the GPU, and the Linux VM Docker needs on
# macOS reserves memory the model cannot spare. See README "Why there is no
# Docker setup".

MODEL ?= qwen2.5-coder-7b
DB    ?= agri_insights

.DEFAULT_GOAL := help
.PHONY: help setup db catalog model serve api web dev test cov eval eval-verify \
        eval-baseline bakeoff clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

setup:  ## Install Python and Node dependencies
	uv sync
	cd web && npm install

db:  ## Create the database, load the CSVs, apply the read-only role
	dropdb --if-exists $(DB)
	createdb $(DB)
	uv run python db/load.py
	psql -d $(DB) -f db/roles.sql

catalog:  ## Regenerate server/catalog.yaml from the live database
	uv run python server/build_catalog.py

model:  ## Download every GGUF in the bake-off roster (~25 GB)
	uv run python model/download.py

serve:  ## Run llama-server for one model: make serve MODEL=qwen3-8b
	./model/serve.sh $(MODEL)

api:  ## Run the agent API on :8000
	MODEL_ID=$(MODEL) uv run uvicorn server.app:app --port 8000 --reload

web:  ## Run the web UI on :5173
	cd web && npm run dev

test:  ## Run the unit tests
	uv run pytest tests/ -q

cov:  ## Run the unit tests with a coverage report
	uv run coverage run -m pytest tests/ -q
	uv run coverage report

eval-verify:  ## Check every gold SQL in the eval set still runs
	PYTHONPATH=. uv run python eval/verify_gold.py

eval:  ## Run the full evaluation harness against MODEL
	PYTHONPATH=. uv run python eval/run_eval.py --model $(MODEL)

eval-baseline:  ## Run the zero-shot baseline against MODEL
	PYTHONPATH=. uv run python eval/run_eval.py --model $(MODEL) --baseline

bakeoff:  ## Evaluate every model in the roster and write the comparison report
	PYTHONPATH=. uv run python eval/bakeoff.py

clean:  ## Remove generated reports, caches and the audit log
	rm -rf eval/reports/*.json audit/ .coverage .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
