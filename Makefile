PYTHON ?= .tools/venv/bin/python
PIP ?= .tools/venv/bin/pip

.PHONY: setup test backend frontend

setup:
	@test -x $(PYTHON) || (echo 'Create the project-local Python 3.12 venv first' && exit 1)
	$(PIP) install --no-build-isolation -e 'backend[test]'

test:
	$(PYTHON) -m pytest backend/tests

backend:
	$(PYTHON) -m uvicorn app.main:app --reload --app-dir backend

frontend:
	cd frontend && npm install && npm run dev
