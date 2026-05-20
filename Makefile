.PHONY: test test-clean demo-up demo-down demo-smoke

test:
	@./tools/run-tests.sh

test-clean:
	@rm -rf .pytest_cache **/__pycache__ 2>/dev/null || true
	@./tools/run-tests.sh

demo-up:
	@./scripts/demo-up.sh

demo-down:
	@./scripts/demo-down.sh

demo-smoke:
	@python3 scripts/demo-smoke.py
