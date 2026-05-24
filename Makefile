.PHONY: lint format test all

lint:
	ruff check .
	ruff format --check .
	mypy factory

format:
	ruff format .
	ruff check --fix .

test:
	pytest -q

all: lint test
