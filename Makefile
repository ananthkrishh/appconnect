PY ?= .venv/bin/python

.PHONY: install up down migrate seed run worker test unit integration lint fmt openapi openapi-diff connector

install:
	python3 -m venv .venv && $(PY) -m pip install -r requirements-dev.txt

up:
	docker compose up -d db redis

down:
	docker compose down

migrate:
	$(PY) manage.py migrate

seed:
	$(PY) manage.py seed_demo

run:
	$(PY) manage.py runserver 8000

worker:
	.venv/bin/celery -A config worker -Q default,webhooks -l info

test:
	$(PY) -m pytest --cov

unit:
	$(PY) -m pytest -m "not integration"

integration:
	$(PY) -m pytest -m integration

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

openapi:
	$(PY) manage.py spectacular --api-version v1 --validate --file openapi/v1.yaml
	$(PY) manage.py spectacular --api-version v2 --validate --file openapi/v2.yaml

openapi-diff:
	git show origin/main:openapi/v2.yaml > /tmp/base-v2.yaml
	$(PY) scripts/openapi_diff.py /tmp/base-v2.yaml openapi/v2.yaml --prefix /api/v2/

connector:
	$(PY) manage.py generate_connector examples/specs/acme_payroll.yaml --app acme-payroll
