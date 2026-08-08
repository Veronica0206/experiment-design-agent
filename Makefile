# Experiment Design Agent Suite — single-command entry points.
# `make release-check` is the complete release gate.

R_SUITES := vera-experiment-designing vera-master-experiment-designing \
            vera-doe-designing vera-indirect-comparing vera-meta-analyzing

.PHONY: test test-r test-py test-integration build deps validate-skills \
	validate-config audit-deps release-check bootstrap

test: test-r test-py

test-r:
	@for s in $(R_SUITES); do \
	  echo "== $$s =="; \
	  Rscript --vanilla $$s/scripts/tests/run_tests.R || exit 1; \
	done

test-py:
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_gates.py
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_verification.py
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_harness.py
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_mcp_client.py
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_audit.py
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_artifact_download.py
	@PYTHONDONTWRITEBYTECODE=1 python3 hooks/tests/test_enforce_verification.py
	@PYTHONDONTWRITEBYTECODE=1 python3 hooks/tests/test_verification_ledger.py
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/tests/test_publish_guard.py
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/tests/test_validate_manifests.py

test-integration:
	@cd mcp-server && npm run test:lifecycle
	@PYTHONDONTWRITEBYTECODE=1 python3 agent-harness/tests/test_mcp_integration.py

build:
	@cd mcp-server && npm run build

deps:
	@cd mcp-server && npm ci

validate-skills:
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/validate_skills.py .

validate-config:
	@PYTHONDONTWRITEBYTECODE=1 python3 tools/validate_manifests.py

audit-deps:
	@cd mcp-server && npm audit --omit=dev --audit-level=high

release-check:
	@$(MAKE) deps
	@$(MAKE) build
	@$(MAKE) test
	@$(MAKE) validate-skills
	@$(MAKE) validate-config
	@$(MAKE) test-integration
	@$(MAKE) audit-deps
	@echo "== release-check: PASS =="

bootstrap:
	tools/bootstrap.sh
