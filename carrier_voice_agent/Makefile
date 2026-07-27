.PHONY: install demo test lint fmt worker initdb docker

install:            ## install deps (incl. dev) into the uv venv
	uv sync --extra dev

demo:               ## run the text-mode simulation (no keys)
	uv run lanevoice-demo

test:               ## run the test suite
	uv run pytest -q

lint:               ## static checks
	uv run ruff check src tests

fmt:                ## auto-format / fix
	uv run ruff check --fix src tests

worker:             ## run the LiveKit worker (dev mode)
	uv run lanevoice-worker dev

initdb:             ## create + seed the SQLite database
	uv run lanevoice-initdb

docker:             ## build the production image
	docker build -t lanevoice:latest .
