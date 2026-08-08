# Hooks

## enforce_verification.py — agent-scoped `Stop` / `SubagentStop` gate

Gives both Claude Code main-agent (`claude --agent ...`) and spawned-subagent
runtimes the same design-level verification the MCP server enforces in code.
`PostToolBatch` records the exact tool inputs and model-visible responses
synchronously, and `Stop` binds the current `last_assistant_message` to those
server-issued verification records. Claude Code automatically treats an
agent-frontmatter `Stop` hook as `SubagentStop` when that agent is spawned.

**Scope.** Each governed agent declares its own hooks in frontmatter and passes
an explicit trusted scope (`experiment-designer` or `design-verifier`) through
the launcher. The launcher rejects an invalid scope or a conflicting runtime
`agent_type`. `.claude/settings.json` intentionally contains no duplicate
global registration.

**Synchronous ledger.** The production hook does not use either transcript
path as a trust source. Claude Code writes transcripts asynchronously, so they
may omit the current turn. The ledger is keyed by `session_id`, `prompt_id`,
and a trusted principal: `main:<scope>` for `claude --agent`, or
`subagent:<scope>:<agent_id>` for a spawned agent. Production and tests use the
same ledger semantics; tests inject an in-memory line source where isolation is
needed. No ambient environment variable can switch the trust source.

**Grouping.** Each `PostToolBatch` event is one attempt group, so a parallel
fan-out is enforced together and never miscounted as several retries.

**Rule.** When either governed agent is about to finish, the hook reads its
per-turn ledger. Gating is **opt-out**: every analysis tool is gated.
`validate_config` is ledgered as a non-statistical pre-check and `run_tests` is
the gate itself; a newly added analysis tool is therefore verified by default.
It blocks (exit 2) if **either**:
1. **Design gate fails** — the server envelope contains a failed or incomplete
   design check, lacks the full regression attestation, or does not match the
   privacy-safe result/report hashes. Enforcement is keyed by a unique runtime-issued
   `verification_id`, normalized arguments, and result identity. Runtime IDs
   are immutable and cannot be reused; model-supplied IDs are ignored, and a
   later unrelated call cannot launder a failure.
   A wrong-direction design is caught here. (Fixable → no loop: the agent
   corrects the config and re-runs, and the new result passes.) Sizing cells
   that legitimately hit the engine's search cap (n = NA) are surfaced as
   *partial*, not failed — infeasibility is a reportable answer, not a defect.
2. **Framework unverified** — no structurally complete `run_tests` result with
   boolean `all_ok=true` and all five expected skill results at or after the
   latest gated result.

Pure elicitation is allowed only through the closed structured clarification
form. A successful `validate_config`-only turn must copy its
`configuration_report` exactly and needs no `run_tests`; that report authorizes
no sample size, effect, probability, or other statistical result.

**Loop safety.** Consecutive failure thresholds permit only
`UNVERIFIED_ESCAPE`, never verification. Failed payloads are withheld by the
MCP server before model exposure. The first terminal stop remains
blocked. On a later stop cycle (`stop_hook_active=true`), the hook allows only
the exact value-free message `Verification failed; results withheld as not
trustworthy.` Normal final answers must exactly copy the server-generated,
privacy-aware `_verification.report`; arbitrary prose, number words, semantic
label swaps, missing hashes, and altered limitation text are rejected.
An unparseable/error tool result is a failed call that a later presentable call
to the same tool may supersede. Invalid ledger structure, a missing response or
batch gap, invalid hook input, unreadable ledgers, and gate-import failures fail closed; after a bounded
stop cycle they may produce only the same value-free `INTERNAL_ERROR` report.

**Registered in** each `.claude/agents/*.md` frontmatter block. A portable shell
wrapper invokes the Node launcher, which resolves `python3` or `python`; every
missing-launcher/interpreter/policy failure is normalized to blocking exit code
2. The generous hook
timeout is not the only trust boundary: the MCP server verifies before release.
It keys on the MCP server name `mcp__experiment-design__` — if you register the
server under a different name in `.mcp.json`, update `SERVER` in the hook.
Claude Code 2.1.197+ is required: the identity-safe ledger depends on the
`prompt_id` hook field introduced in 2.1.196, and both governed agents select
Claude Sonnet 5, which requires 2.1.197. `tools/bootstrap.sh` enforces the
combined minimum.

**Tests.** `hooks/tests/test_enforce_verification.py` covers identity-safe
correction, laundering, framework schemas, and value-free escape behavior.
`hooks/tests/test_verification_ledger.py` exercises the production event path,
including incomplete batches and concurrent writers. Hooks read event JSON on stdin and
exit 0 (allow) or 2 (block).

## Observability — proving the hook actually ran

A blocking hook is loud, but an **allowing** hook is silent, so "the hook ran
and allowed" is indistinguishable from "the hook never ran". That ambiguity is
exactly how the earlier silent no-op survived (it read the parent session
transcript, found no subagent tool calls, and allowed every stop).

Every invocation is recorded by default in a private temporary log. Set
`EXPDESIGN_HOOK_LOG` to choose another path:

```bash
export EXPDESIGN_HOOK_LOG=/tmp/expdesign-hook.log
```

Each line is `ALLOW|BLOCK <TAB> reason_sha256=<digest>`. The reason itself is
not persisted, so verification details cannot leak through diagnostics.

```
ALLOW	reason_sha256=7c1b...f09a
BLOCK	reason_sha256=83d4...1e20
```

An empty log after a governed agent run means the hook was **never invoked** —
check the hook command and scope in that agent's frontmatter.
Logging failures never change the enforcement decision.
