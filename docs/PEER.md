# Call Claude and continue your task

The source preview adds `relay peer`: a coding agent calls Claude Code, receives
its answer as the command result, and continues the same task. There is no second
application or message-forwarding service. This command is included in bundles
built from this source; **the published v0.1.0 archive does not contain it**.

Use it when a second perspective is worth the extra provider usage. The first
adapter calls Claude; it does not start a replacement Codex task, control an
already-open desktop window, or wake an independently idle session.

## Before calling

Use a functioning provider environment and an enrolled checkout. Follow
[installation](engineering/INSTALLATION.md#build-a-local-preview-bundle) and
[setup](SETUP.md). A source-built installation also includes `relay launch`
for interactive Codex/Claude launches; `relay launch claude --json` only prepares
that interactive launch plan.

The peer flags are available in Claude Code 2.1.267; `--permission-prompts none`
requires 2.1.259 or later. Version compatibility is not a guarantee of native
tool readiness. Review the chosen repository and provider configuration first:
Claude print mode loads normal instructions, hooks, skills and configured MCP
servers, and does not show its interactive workspace trust dialog.

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

`--json` returns structured output **and executes the call**. Add `--dry-run`
to inspect the task hash and native arguments without starting Claude or writing
call evidence. Unlike `relay launch`, an explicit peer call has no additional
interactive confirmation prompt. Provider usage must already be authorized.
Task text goes through stdin as data, never shell evaluation or command-line
prompt interpolation. Use `--task-file -` to supply it from stdin directly.

The task limit is 64 KiB. Larger artifacts belong in the repository and can be
referenced by the task. The default timeout is 600 seconds; use `--timeout` to
adjust it for the work. Relay imposes no native turn cap by default. Supply
`--max-turns` when you want an explicit turn limit. Choose bounds proportionate
to the task so the peer has time to inspect evidence and produce a useful
answer. Relay makes one invocation and never automatically retries it.

Normal permission rules remain in force. When a tool needs approval that this
noninteractive call cannot obtain, Claude denies it and reports the denial.
Relay retains useful partial output. The initiating agent should report any
required human decision; it must not silently widen permissions to finish.

## Read the result before continuing

| Field or state | Meaning |
|---|---|
| `returned` | A matching final provider result arrived without a provider/process error. Assess its content; this is not acceptance of the task. |
| `provider_error` | Claude returned a matching error or limit result, or exited nonzero. Partial text and bounded `provider_errors` remain available. |
| `unavailable` | Preparation or spawning failed; `provider_started` and `unavailable_stage` identify what was reached. |
| `uncertain` | The call started but timed out, was interrupted, or lacked a valid matching result. External work may already have happened. |
| `needs_attention`, `permission_denials`, `terminal_reason` | Check these even when the process exits zero. Tool denials and stopped/deferred work can accompany useful output. |
| `session_id` | The verified returned Claude session identity. `requested_session_id` is recorded before launching for recovery. |
| `relay_acknowledgement`, `workflow_completion` | Always `not_checked` by the helper. Inspect actual ledger state and artifacts separately. |

Each call retains a private directory containing its request, task, native
stdout/stderr and interpreted result. Use `--output-dir /absolute/new/directory`
to choose a durable location; an existing directory is refused without changes.
The default is a retained temporary directory, subject to the OS's cleanup
policy. Its location and the requested session ID are printed before launch.
Keep these files private: native output and task text can contain sensitive
project information. Nothing is uploaded or published by Relay's recorder.

After timeout or an uncertain result, inspect the local evidence and durable
work before deciding whether to continue. Relay stops only the process group
created for that call. Killing a process does not prove that prior external
operations were undone. Never release someone else's claim to tidy the result.

For an intentional follow-up, use the returned UUID and the same checkout:

```sh
relay peer claude --repo /absolute/enrolled/reviewer-checkout \
  --resume <exact-session-uuid> --task-file follow-up.txt --json
```

There is no latest-session lookup or automatic concurrent resume. A fresh call
without `--resume` creates a fresh session. A resume must be a deliberate choice
after checking the previous outcome, including any partial work.

The response includes measured call duration and provider-reported usage/turns
when available. `estimated_cost_usd` is a provider estimate, not an observed
subscription charge. Missing measurements remain unknown. These measurements
do not establish net token savings or broad reliability.

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
keeps its original scope; it does not establish this source preview's complete
automatic round-trip. A native verification of this candidate is still pending.

For source development, `examples/call_peer.py` invokes the same helper against
a reviewed installed Relay selected with `--relay`. This is a source entry point,
not proof that a different installed runtime contains the new command.
