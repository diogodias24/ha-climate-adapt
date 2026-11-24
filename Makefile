.PHONY: install lint type-check security format check fix clean test coverage all

install:
	pip install -r requirements_dev.txt

lint:
	.venv/bin/ruff check custom_components/ tests/

type-check:
	.venv/bin/mypy custom_components/

security:
	.venv/bin/bandit -r custom_components/ -q

format:
	.venv/bin/black custom_components/ tests/
	.venv/bin/isort custom_components/ tests/

check: lint type-check security
	@echo "All checks passed!"

fix:
	.venv/bin/ruff check --fix custom_components/ tests/
	.venv/bin/black custom_components/ tests/
	.venv/bin/isort custom_components/ tests/

test:
	.venv/bin/pytest tests/ -v

coverage:
	.venv/bin/pytest tests/ -v --cov=custom_components/climate_adapt --cov-report=term-missing --cov-report=html

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage

all: check test
