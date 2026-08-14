# Hooks

## Coordinator prompt capture and dispatch binding

The native coordinator is a **main-agent-only** entrypoint. Run it with
`claude --agent experiment-design-coordinator`. Current Claude Code releases
can nest subagents, but the `Agent(child-a, child-b)` type allowlist is enforced
only for a main agent; inside a subagent definition the parenthesized list is
ignored. Requiring the coordinator as main preserves both its child allowlist
and its user-prompt binding.

Before each main-thread user turn, the coordinator's `UserPromptSubmit` hook
captures a keyed HMAC commitment to the exact raw prompt. The commitment is
keyed by `session_id`, `prompt_id`, and `main:experiment-design-coordinator`.
The raw prompt is never written to disk. The commitment ledger, key, and lock
files use mode `0600` inside a mode-`0700` host directory.

Before an `Agent` call executes, the coordinator's `PreToolUse` hook requires:

1. the exact committed user prompt, byte-for-byte;
2. one registry-approved child type;
3. the deterministic description `Dispatch current request to <child>`;
4. explicit foreground execution (`run_in_background: false`); and
5. no extra model, resume, follow-up, naming, isolation, or team fields;
6. at most one dispatch to each child for this prompt; and
7. one registry-domain-derived phase (`evidence` or `planning`) for the turn.

A missing capture, prompt paraphrase, cross-turn replay, nested-coordinator
principal, repeated child, evidence/planning mix, asynchronous call, or
unsupported input shape blocks before the child starts. Child names already
authorized for the turn are committed atomically without storing the prompt;
distinct same-phase fan-out remains allowed. The per-prompt commitment is
removed when the coordinator emits a terminal governed response.
`PostToolBatch` still sanitizes the completed child call to its approved type
plus final text, and `Stop` still binds the coordinator's final output to that
ledger. A legacy/malformed mixed ledger may terminate only with the exact
value-free failure report, which also clears the turn state.

## enforce_verification.py — agent-scoped `Stop` / `SubagentStop` gate

Gives both Claude Code main-agent (`claude --agent ...`) and spawned-subagent
runtimes fail-closed enforcement of the MCP server's design verdict. The private
raw-result checks run once inside the server verifier; the hook does not receive
that raw payload and does not recompute them. Instead, `PostToolBatch` records the
exact tool inputs and model-visible responses synchronously, and `Stop` validates
the server-bound public envelope and attestation, requires fresh regression
evidence, and binds the current `last_assistant_message` to the server-issued
canonical report. Claude Code automatically treats an
agent-frontmatter `Stop` hook as `SubagentStop` when that agent is spawned.

**Scope.** Each governed agent declares its own hooks in frontmatter and passes
an explicit trusted registry scope through the launcher. The launcher rejects
an invalid scope or a conflicting runtime `agent_type`; it also rejects a
coordinator carrying a subagent `agent_id`. `.claude/settings.json`
intentionally contains no duplicate global registration.
The launcher imports a fixed, frozen domain-tool authorization table rather than
trusting the mutable registry alone. A separate validator-only inspector reads
that same side-effect-free policy module; `validate-config` requires exact parity
with both `governance/agents.json` and the Python registry boundary. The
production launcher recognizes only `capture`, `bind`, `record`, and `enforce`,
so policy inspection can never act as a successful Hook mode; the runtime still
independently fails closed.

**Synchronous ledger.** The production hook does not use either transcript
path as a trust source. Claude Code writes transcripts asynchronously, so they
may omit the current turn. The ledger is keyed by `session_id`, `prompt_id`,
and a trusted principal: `main:<scope>` for `claude --agent`, or
`subagent:<scope>:<agent_id>` for a spawned agent. Production and tests use the
same ledger semantics; tests inject an in-memory line source where isolation is
needed. No ambient environment variable can switch the trust source. The
ledger root must be a private, owned, non-symlink directory; lock and data
files are opened with no-follow semantics and must be owned regular files.

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

**Registered in** each `.claude/agents/*.md` frontmatter block with exact
`/bin/sh`, never ambient `sh`. The portable shell wrapper never resolves Node
through ambient `PATH`: it accepts an explicitly
reviewed absolute `EXPDESIGN_NODE` or one of the fixed system/package-manager
locations, then the Node launcher selects an approved absolute Python and
strips Python startup/profile variables. Every
missing-launcher/interpreter/policy failure is normalized to blocking exit code
2. The project MCP declaration invokes absolute `/bin/sh`, then its dedicated
launcher applies the same fixed-or-explicit-absolute Node policy. Neither
governed launch path inherits a Node executable from ambient `PATH`.
The generous hook
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
including incomplete batches and concurrent writers.
`hooks/tests/test_coordinator_verification.py` covers exact prompt binding,
private persistence, replay/paraphrase attacks, strict foreground Agent input,
child-result fan-in, and coordinator stop enforcement. Hooks read event JSON on
stdin and exit 0 (allow) or 2 (block).

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

Each line is a private-permission JSON event containing the timestamp,
monotonic sequence token, session/prompt identifiers when supplied by the host,
governed principal, hook event, and `ALLOW`/`BLOCK` outcome. Gate reasons and
deterministic reason hashes are deliberately omitted because low-entropy values
remain guessable from unsalted hashes.

```
{"hook_event":"Stop","outcome":"ALLOW","principal":"main:experiment-designer","schema_version":1,"sequence":123,"timestamp":123.0}
```

An empty log means either the hook was not invoked or observability failed; it
must not be treated as proof of one specific cause. Set
`EXPDESIGN_HOOK_LOG_REQUIRED=1` in environments where an unwritable trace must
block an otherwise allowing decision. By default, logging failures do not change
the enforcement decision.
