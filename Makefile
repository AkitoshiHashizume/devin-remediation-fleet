.PHONY: up demo demo-docker install down clean reset

install:
	python3 -m pip install -r requirements.txt

# Real mode: requires .env with DEVIN_API_KEY / DEVIN_ORG_ID / GITHUB_TOKEN
up:
	docker compose up --build

# Keyless demo: full flow (webhook -> state machine -> dashboard) in mock mode
demo:
	bash scripts/demo.sh

# Same keyless demo, fully containerized (no local Python needed)
demo-docker:
	docker compose --profile demo up --build demo

# Stop everything: compose stacks (all profiles) and any locally-run server
down:
	-docker compose --profile smee --profile demo down
	-pkill -f "uvicorn app.main:app"

# Wipe all local run data (SQLite state, raw API responses, logs)
clean:
	rm -rf data/

# Back to a pristine checkout: stop everything, then wipe run data
reset: down clean
