# Experiment Design Agent Suite — single-command entry points.
# `make release-check` is the complete gate for an authorized installation.

R_SUITES := vera-experiment-designing vera-master-experiment-designing \
            vera-doe-designing vera-indirect-comparing vera-meta-analyzing
PYTHON ?= $(abspath agent-harness/.venv/bin/python)
PYTHON_RUN = EXPDESIGN_PYTHON="$(PYTHON)" tools/run-reviewed-python.sh
NPM_INSTALL ?= npm ci --no-audit --no-fund

.PHONY: test test-r test-py test-integration build deps validate-skills \
	validate-config validate-r-lock validate-python-lock audit-deps check-claude-version \
	check-claude-live public-check harness-release-check release-check bootstrap

test: test-r test-py

test-r:
	@for s in $(R_SUITES); do \
	  echo "== $$s =="; \
	  tools/run-reviewed-r.sh $$s/scripts/tests/run_tests.R || exit 1; \
	done
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_planning_consistency.R
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_posterior_contract.R

test-py:
	@$(PYTHON_RUN) agent-harness/tests/test_gates.py
	@$(PYTHON_RUN) agent-harness/tests/test_verification.py
	@$(PYTHON_RUN) agent-harness/tests/test_single_endpoint_contract.py
	@$(PYTHON_RUN) agent-harness/tests/test_harness.py
	@$(PYTHON_RUN) agent-harness/tests/test_multi_agent.py
	@$(PYTHON_RUN) agent-harness/tests/test_streamlit_surface.py
	@$(PYTHON_RUN) agent-harness/tests/test_mcp_client.py
	@$(PYTHON_RUN) agent-harness/tests/test_response_budget.py
	@$(PYTHON_RUN) agent-harness/tests/test_audit.py
	@$(PYTHON_RUN) agent-harness/tests/test_artifact_download.py
	@$(PYTHON_RUN) hooks/tests/test_enforce_verification.py
	@$(PYTHON_RUN) hooks/tests/test_coordinator_verification.py
	@$(PYTHON_RUN) hooks/tests/test_verification_ledger.py
	@$(PYTHON_RUN) tools/tests/test_publish_guard.py
	@$(PYTHON_RUN) tools/tests/test_validate_manifests.py
	@$(PYTHON_RUN) tools/tests/test_validate_python_environment.py
	@$(PYTHON_RUN) tools/tests/test_reviewed_python_runner.py
	@$(PYTHON_RUN) tools/tests/test_sanitize_python_environment.py
	@$(PYTHON_RUN) tools/tests/test_validate_skills.py
	@$(PYTHON_RUN) tools/tests/test_validate_r_lock.py
	@$(PYTHON_RUN) tools/tests/test_dependency_advisories.py

test-integration:
	@cd mcp-server && npm run test:lifecycle
	@$(PYTHON_RUN) agent-harness/tests/test_mcp_integration.py

build:
	@cd mcp-server && npm run build

deps:
	@cd mcp-server && npm ci

# The exported single-endpoint edition runs real statistical and MCP checks.
# Full-source tests remain in release-check; no missing suite is skipped there.
public-check:
	@tools/run-publication-python.sh "$(CURDIR)/tools/validate_public_distribution.py" --public-workflow "$(CURDIR)/.github/workflows/public-assurance.yml"
	@tools/run-publication-python.sh "$(CURDIR)/tools/validate_public_distribution.py" --public-clone "$(CURDIR)"
	@$(PYTHON_RUN) tools/prepare_public_release.py --check "$(CURDIR)"
	@$(MAKE) validate-python-lock
	@$(PYTHON_RUN) tools/validate_manifests.py
	@cd mcp-server && $(NPM_INSTALL)
	@cd mcp-server && npm run test:public-lifecycle
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/run_tests.R
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_planning_consistency.R
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_posterior_contract.R
	@tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/public_packaging.R
	@$(PYTHON_RUN) agent-harness/tests/test_single_endpoint_contract.py
	@$(PYTHON_RUN) tools/tests/test_dependency_advisories.py
	@$(PYTHON_RUN) agent-harness/tests/test_runtime_profile.py
	@$(PYTHON_RUN) tools/tests/test_public_release.py
	@cd mcp-server && EXPDESIGN_PYTHON="$(PYTHON)" npm run test:public-engine
	@echo "== public-check: PASS (single-endpoint engine, verification and public boundaries) =="

validate-skills:
	@$(PYTHON_RUN) tools/validate_skills.py .

validate-config:
	@$(PYTHON_RUN) tools/validate_manifests.py

validate-r-lock:
	@$(PYTHON_RUN) tools/validate_r_environment.py

validate-python-lock:
	@$(PYTHON_RUN) --validate-only

audit-deps:
	@cd mcp-server && npm audit --omit=dev --audit-level=high
	@$(PYTHON_RUN) tools/audit_dependency_advisories.py

check-claude-version:
	@tools/bootstrap.sh --check-claude-version

# Optional provider-readiness check. This requires an authenticated Claude Code
# session and is intentionally separate from the release gate.
check-claude-live:
	@tools/bootstrap.sh --check-claude-live

# Claude-independent gate for the Python/MCP harness in an authorized complete
# installation. This does not claim that Claude Code's agent-scoped hooks are
# runnable on the host.
harness-release-check:
	@$(MAKE) deps
	@$(MAKE) build
	@$(MAKE) validate-python-lock
	@$(MAKE) test
	@$(MAKE) validate-skills
	@$(MAKE) validate-config
	@$(MAKE) validate-r-lock
	@$(MAKE) test-integration
	@$(MAKE) audit-deps
	@echo "== harness-release-check: PASS =="

# Complete agent release gate: enforce the minimum installed Claude Code version,
# then run the authorized-installation harness matrix. This gate does not
# require provider authentication, but dependency installation and
# vulnerability audit steps do require registry access. Check a live provider
# session separately with `make check-claude-live`.
release-check:
	@$(MAKE) check-claude-version
	@$(MAKE) harness-release-check
	@echo "== release-check: PASS =="

bootstrap:
	tools/bootstrap.sh
