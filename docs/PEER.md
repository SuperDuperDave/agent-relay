# Call another native provider and continue your task

Multithread (formerly Agent Relay) includes `relay peer`: a coding agent calls
another native provider, receives its answer as the command result, and continues
the same task. There is no second application or message-forwarding service.

**v0.3 source preview.** Adds Codex calls and live input. Check the selected
release's version and verification results before installation. The earlier
v0.2.0 release supports Claude call/return and exact-session resume; its archive
lacks the v0.3 additions. The historical v0.1.0 archive has no peer command.
Use the [setup guide](SETUP.md) for a published release, or the
[source entry](#use-the-source-entry)
for the new commands with an existing v0.2.0 installation.

The installed command remains `relay`. See the
[project page](https://mainthread.ai/work/relay/) for the Multithread introduction.

Use it when a second perspective is worth the extra provider usage. Either
agent can call Claude or Codex. The initiating agent keeps the continuing goal.
This command does not control an already-open desktop window or wake an
independently idle session.

## Before calling

Use a functioning provider environment and an enrolled checkout. Follow
[setup](SETUP.md). The installation also includes `relay launch`
for interactive Codex/Claude launches; `relay launch claude --json` only prepares
that interactive launch plan.

The peer flags are available in Claude Code 2.1.267; `--permission-prompts none`
requires 2.1.259 or later. Version compatibility is not a guarantee of native
tool readiness. Review the chosen repository and provider configuration first:
Claude print mode loads normal instructions, hooks, skills and configured MCP
servers, and does not show its interactive workspace trust dialog.
The Codex adapter uses the stable App Server interface. The v0.3 native
source-entry observations use Codex 0.153.4 and Claude Code 2.1.269. Codex hook
trust remains a separate native review: use `relay launch codex` for the selected
checkout, open `/hooks`, and review the exact Relay commands. A changed hook
definition can need review again. Listing a trusted hook does not prove it ran.

Relay inherits the provider's normal environment, sign-in and permission mode.
It never selects bare mode, copies credentials, changes permission rules or
disables native sandboxing. Subscription sign-in can be used; an API key or
other provider configuration can change the effective billing path. Check it
through the provider's normal interface. Relay does not certify account billing.
[Claude programmatic use](https://code.claude.com/docs/en/headless) ·
[Authentication](https://code.claude.com/docs/en/authentication)

## Send a scoped task

Write a small UTF-8 task file with the goal, scope and acceptance criteria. For
example, after publishing a commit-backed Relay handoff for Claude:

```text
Review Relay handoff <sequence> for work <work-id>.
Inspect the exact commit and relevant tests. Work only in this checkout and
within the approved review scope. Report actionable defects with evidence,
or explicitly say no material findings. Acknowledge the handoff after review;
release any claim you acquired. Report anything that remains incomplete.
```

Use the installed launcher's actual path if `relay` is not on PATH:

```sh
relay peer claude --repo /absolute/enrolled/reviewer-checkout \
  --task-file review-task.txt --json
```

With a reviewed v0.3 installation, select `codex` in the same command
to call Codex. For a v0.2.0 installation, use the source entry below.
Either initiating provider can use these commands through its ordinary shell
tools. The caller's provider does not determine the peer's provider.

`--json` returns structured output **and executes the call**. Add `--dry-run`
to inspect the task hash and native arguments without starting a provider or writing
call evidence. Unlike `relay launch`, an explicit peer call has no additional
interactive confirmation prompt. Provider usage must already be authorized.
Task text goes through stdin as data, never shell evaluation or command-line
prompt interpolation. Use `--task-file -` to supply it from stdin directly.

The task limit is 64 KiB. Larger artifacts belong in the repository and can be
referenced by the task. The default timeout is 600 seconds; use `--timeout` to
adjust it for the work. Relay imposes no native turn cap by default. Supply
`--max-turns` for an explicit Claude turn limit. Codex performs one native turn
with its normal tool loop. Choose bounds proportionate
to the task so the peer has time to inspect evidence and produce a useful
answer. Relay makes one invocation and never automatically retries it.

Normal permission rules remain in force. When a tool needs approval that this
noninteractive call cannot obtain, Claude denies it and reports the denial.
For Codex, configured native approval review remains in effect; requests routed
to this unattended client are declined. Relay never supplies extra permissions.
Relay retains useful partial output. The initiating agent should report any
required human decision; it must not silently widen permissions to finish.

## Read the result before continuing

| Field or state | Meaning |
|---|---|
| `returned` | A matching final provider result arrived. Assess its content and `needs_attention`; a later streaming-process or recording fault does not erase an observed answer. This is not acceptance of the task. |
| `provider_error` | A matching native error, interruption or limit result. Legacy Claude calls also classify a nonzero process exit this way. Useful text and bounded `provider_errors` remain available. |
| `unavailable` | Preparation or spawning failed; `provider_started` and `unavailable_stage` identify what was reached. |
| `uncertain` | The call started but timed out, was interrupted, or lacked a valid matching result. External work may already have happened. |
| `needs_attention`, `permission_denials`, `terminal_reason` | Check these even when the process exits zero. Tool denials and stopped/deferred work can accompany useful output. |
| `session_id` | The verified native session identity: a UUID for Claude, an opaque native thread ID for Codex. Claude's requested UUID is recorded before launching; Codex assigns a fresh thread ID during initialization. Resume always targets the exact supplied identity. |
| `observed_session_id` | If present on an identity mismatch, the unverified native identity reported by the provider. It is diagnostic, not a resume instruction; inspect the retained raw output. |
| `relay_acknowledgement`, `workflow_completion` | Always `not_checked` by the helper. Inspect actual ledger state and artifacts separately. |

Each call retains a private directory containing its request, task, native
stdout/stderr and interpreted result. Use `--output-dir /absolute/new/directory`
to choose a durable location; an existing directory is refused without changes.
The default is a retained temporary directory, subject to the OS's cleanup
policy. Its location is printed before launch, together with Claude's requested
session UUID or a note that Codex will assign the identity.
Keep these files private: native output and task text can contain sensitive
project information. Nothing is uploaded or published by Relay's recorder.
`stdout_observation` records the byte count and SHA-256 of observed stdout.
For Codex and Claude `--live-input`, capture is limited to 16 MiB, plus one byte
to detect overflow. Exceeding that bound sets `truncated`, stops interpretation,
closes native input and leads to owned-process cleanup. Only the captured prefix
is retained; a previously observed answer survives with `needs_attention`.
Claude calls without `--live-input` capture raw stdout directly and apply the
16 MiB limit when reading it for interpretation. Their raw file can exceed that
summary limit. Native stderr and directly captured stdout are not sealed: a
provider descendant may still hold their file descriptors. Compare observed
bytes before reusing a summary if they changed.

After timeout or an uncertain result, inspect the local evidence and durable
work before deciding whether to continue. Relay stops only the process group
created for that call. Killing a process does not prove that prior external
operations were undone. Never release someone else's claim to tidy the result.
SIGINT, SIGTERM and SIGHUP also trigger owned-process cleanup and an uncertain
receipt unless a valid streaming result has already been observed or the caller
already ignores that signal (for example, SIGHUP
under `nohup`). Once the provider exits, ordinary signals allow the bounded
result read and receipt to finish. SIGKILL, host failure and a provider that
escapes that process group cannot be handled this way.

A streaming result can precede native process exit. Claude receives the remaining
call time for its background work; Codex's owned server gets a short shutdown
allowance after its terminal turn. Cleanup is limited to the process group this
call created. A valid answer survives incomplete stdout observation, marked with
`needs_attention`. Neither a terminal result nor termination proves that every
background operation completed.

For an intentional follow-up, use the exact returned native session identity
and the same checkout. Claude uses a session UUID:

```sh
relay peer claude --repo /absolute/enrolled/reviewer-checkout \
  --resume <exact-session-uuid> --task-file follow-up.txt --json
```

For Codex in v0.3, select `codex` and pass its returned `session_id`
unchanged to `--resume`. Treat that thread ID as opaque; do not convert it to a
UUID or substitute `native_session_id`.

There is no latest-session lookup or automatic concurrent resume. A fresh call
without `--resume` creates a fresh session. A resume must be a deliberate choice
after checking the previous outcome, including any partial work.

The response includes measured call duration and provider-reported usage/turns
when available. `estimated_cost_usd` is a provider estimate, not an observed
subscription charge. Missing measurements remain unknown. These measurements
do not establish net token savings or broad reliability.

## Update a running peer

Live input applies to a call that Relay owns and that is still running. Codex
exposes input for its exact active turn. For Claude, add `--live-input` when
starting the call: an update can be picked up between tool calls or become a
later turn in the same session. This opt-in can use additional provider turns;
the original wall-time limit still applies to the whole call. It is not a
promise that an update will affect work already in progress.

These commands require v0.3. Use the source entry described below
in place of `relay peer` when your installed runtime is v0.2.0.

Choose `--output-dir` when launching so another cooperating process can locate
the call while the initiating tool waits. The directory must not already exist.
Inspect its current input target:

```sh
relay peer control status --call-dir /absolute/peer-call --json
```

Use the advertised session and turn to send a Codex update:

```sh
relay peer control send --call-dir /absolute/peer-call \
  --session <session-id> --turn <turn-id> --message-file update.txt --json
```

For a Claude call started with `--live-input`, use the same command with its
exact session and **omit `--turn`**. The helper refuses a different target; it
does not choose a latest session, reopen a call, or forward to an unrelated
desktop task. Native provider permissions remain in force.

| Input receipt | What it establishes |
|---|---|
| `pending` | The local request is retained, with no final native observation yet. The command exits 2 after a short wait and prints an inspection command. |
| `accepted` | Codex acknowledged the exact active-turn update. Consumption is not verified. |
| `consumed` | Claude's main-session output explicitly named this message UUID among the answered inputs. This does not establish task completion. |
| `rejected` | The input was refused locally or by the native provider. The original task may still return successfully. |
| `uncertain` | A dispatched input lacks the necessary native observation. Preserve it and inspect before any deliberate follow-up. |
| `unavailable` | The helper could not establish or record an input observation. This is not proof that nothing was sent. |

`send` prints a request UUID before submission. Keep it if the reply is lost.
The same UUID with identical content inspects the existing request; it never
resends dispatched input. Reusing it with different content is refused. Use
`receipt --call-dir … --request-id … --json` to inspect later. Final receipts
are immutable; a pending receipt can gain an observation while the owner runs.

Input acceptance closes at Codex's terminal turn or Claude's first result
answering this call's submitted input.
Already dispatched Claude updates can still produce subsequent results. Missing
message attribution does not settle the initial task or a queued update.
`native_results` retains bounded separate observations, including background
results. The input UUIDs attributed by a result determine whether it answers this
call's submitted input; an unrelated result cannot replace the task's answer.
An input echo proves only receipt, and a subagent's answer cannot satisfy the
main-session task.

The mailbox is private local coordination among cooperating processes under the
same OS account. It is not agent authentication or a background service. An open
target file alone does not prove its owner remains alive after a crash. Relay
does not automatically replay pending work on restart. Per-call bounds keep this
channel finite; inspect explicit refusals rather than opening replacement calls
to evade them.

If the input mailbox becomes unavailable, new dispatch stops while the original
authorized task can continue. Check `control_fault` and the private receipts;
the availability of a receipt and the native result are distinct observations.

Claude streaming reports per-result usage separately from the latest cumulative
`model_usage` and cost estimate. Cumulative totals must not be added across
results. Provider-reported counts have their native scope and do not measure
human effort, actual subscription billing, or the caller's own usage.

## Coordination and verification scope

The helper obtains the existing public hook configuration through the admitted
installed worker. The provider itself runs outside that worker's restricted
environment, using its ordinary tools and sandbox. Hooks can report unavailable
observations without blocking Claude; `hook_delivery` therefore stays unknown
until independently observed. The helper never claims, acknowledges, commits,
merges or releases on a participant's behalf.

Automated checks cover fake native processes, refusal and partial-result cases,
exact sessions, interrupted calls, private evidence, and installed commands with
the source checkout absent and real public hooks. They are not real-model
workflow evidence. The [historical native workflow](PROVIDERS.md#bounded-native-workflow)
keeps its original scope. A separate development check with Claude Code 2.1.267
used the public source entry and an existing installed development runtime:
Claude wrote a code review, acknowledged the exact handoff and released its
claim; its answer returned to the initiating Codex task without manual message
forwarding. The caller inspected the artifact and ledger and acted on the review.
The call took 520 seconds and 13 provider turns, with no reported permission
denials. That observation applies to the source entry.

The reviewed bundle was then installed through the normal account-level upgrade.
The initiating Codex task used the installed `relay peer` command to resume that
exact Claude session for a review of the fixes. Claude wrote a separate follow-up
artifact, acknowledged its new handoff and released its claim. The matching
result returned automatically in 359 seconds, with 12 provider-reported turns
and no reported permission denials. The caller independently checked both review
artifacts, unchanged input commits and ledger events. This verifies installed
call/return and native exact-session resume on that existing WSL2/ext4 developer
profile with Claude Code 2.1.267. Subsequent signal refinements have separate
regression evidence; they were not exercised by terminating a paid review.

The v0.3 source has separate native observations on the existing
developer profile with Codex 0.153.4 and Claude Code 2.1.269. Codex acknowledged
an update to its exact active turn, and its review artifact reflected that
update. Claude's main-session output named the submitted live-input UUIDs as
consumed. The first reverse workflow was interrupted by a bridge cleanup defect;
its evidence remains retained.

A deliberate resume of the same Claude session then led a resume of the exact
Codex thread. Codex returned a follow-up, wrote `codex-followup.md` and released
its own claim. Claude wrote an independent report and emitted a native
background final result. The original helper receipt for that Claude resume
remained `uncertain`: the bridge rejected repeated initialization of the same
session. Completed artifacts and that native result do not turn the original
receipt into a successful helper return.

After the bridge correction, an offline replay of the retained native stream
accepted its repeated same-session initialization, returned the result
attributed to the submitted input and retained the later background report as a
separate observation. The original receipt remains unchanged. Replay verifies
interpretation of those retained bytes; it is not a newly executed live workflow.
Independent ledger inspection found no active claims, including the released
Codex claim; no handoff acknowledgement was due for that follow-up.
These observations do not establish installed-native v0.3 support.

These observations do not establish independent onboarding, broad reliability,
net usage savings or waking an independently idle task. The source-absent
installation checks remain separate from the normal native runs, which did not
hide source files or alter provider capabilities for isolation.

## Use the source entry

From the reviewed source checkout, `examples/call_peer.py` invokes the same
helper against a reviewed installed Relay selected with `--relay`:

```sh
/usr/bin/python3 -I -S -B examples/call_peer.py codex \
  --relay /absolute/reviewed/relay --repo /absolute/enrolled/peer-checkout \
  --task-file task.txt --output-dir /absolute/new-peer-call --json
```

Select `claude` and add `--live-input` for Claude session input. In control
commands, replace `relay peer` with the same source entry:

```sh
/usr/bin/python3 -I -S -B examples/call_peer.py control status \
  --call-dir /absolute/new-peer-call --json
/usr/bin/python3 -I -S -B examples/call_peer.py control receipt \
  --call-dir /absolute/new-peer-call --request-id <request-uuid> --json
```

Use that substitution for `control send` too. Printed installed-launcher
inspection commands require a runtime that contains v0.3 control support;
with v0.2.0, inspect through the source entry instead. Source-entry execution
does not prove that the installed runtime contains these additions.
