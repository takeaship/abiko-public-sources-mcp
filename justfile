set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    scripts/setup.sh
    uv sync --frozen

up:
    docker compose up -d
    uv run python scripts/ready.py

stop:
    docker compose down

mcp:
    uv run python server.py

smoke:
    uv run python scripts/benchmark.py --smoke

bench:
    uv run python scripts/benchmark.py --repeats 3

details:
    uv run python scripts/benchmark.py --repeats 3 --only-docs bill_pdf child --questions evaluation/followup-questions.json

check:
    uv run ruff check server.py store.py scripts tests
    uv run pytest -q
