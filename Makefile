PYTHON ?= python
NPM ?= npm
SIDECAR_HOST ?= 127.0.0.1
SIDECAR_PORT ?= 8765

.PHONY: dev-sidecar build-plugin test contracts contract-test python-test plugin-test plugin-typecheck

dev-sidecar:
	cd services/sidecar && PYTHONPATH=src $(PYTHON) -m clawtune_sidecar.main --host $(SIDECAR_HOST) --port $(SIDECAR_PORT)

build-plugin: plugin-typecheck
	cd packages/clawtune-plugin && $(NPM) run build

test: contract-test python-test plugin-test

plugin-test:
	cd packages/clawtune-plugin && $(NPM) test

plugin-typecheck:
	cd packages/clawtune-plugin && $(NPM) run typecheck

contracts:
	$(PYTHON) tools/validate_contracts.py

contract-test: contracts

python-test:
	cd services/sidecar && $(PYTHON) -m pytest --basetemp ../../.pytest-tmp
