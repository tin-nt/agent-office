# Managed OpenCode agents

This is an opt-in control plane for **new** OpenCode sessions. It is separate
from the observer adapters: the observer continues to only record activity, and
it never answers permissions, sends prompts, or creates sessions.

Start Agent Office with one or more allowlisted project roots:

```sh
python3 run.py --enable-opencode-agents --project-root "/absolute/path/to/projects"
```

Open **Agent chat** in the toolbar. The panel discovers the running local
OpenCode service through its private service registration, creates a session
for the selected project, and keeps the session's conversation in OpenCode.
Agent Office stores only bindings, loop counters, and pause state in
`agents.json` under the office data directory. It does not store transcripts,
prompts, the OpenCode password, or model responses.

## Contract

- **One project per agent.** A managed agent is bound to exactly one
  allowlisted project and one OpenCode session. A project can have only one
  actively running managed agent at a time.
- **Allowlisted roots.** `--project-root` is repeatable. Project paths are
  resolved and must be inside one of those roots. Symlinks are resolved before
  the check.
- **Opt-in.** Without `--enable-opencode-agents`, the panel reports that the
  control plane is disabled. The ordinary observer, task runner, and public
  demo do not expose it.
- **No source patch.** The control plane uses OpenCode's public V2 HTTP API.
  It requires OpenCode 2.2.0 or newer.
- **No automatic approval.** Permission requests and forms are shown in the
  panel. The agent waits for your answer. Rejecting a permission is a normal
  answer, not an error. Form fields with `when` conditions hide and show as
  answers change; hidden fields are omitted from the submitted answer.
- **No transcript persistence.** Reloading Agent Office re-reads the session
  from OpenCode. A restart pauses every active loop and requires an explicit
  resume.

## Continuous mode

The default mode is **continuous**. After the initial objective, the loop sends
a continuation prompt whenever the session becomes idle. It keeps working until
one of these stop rules fires:

- `/goal` in the panel or in the OpenCode TUI.
- The token budget (`input + cache.write + output + reasoning`; cache reads are
  reported separately and excluded by default). Descendant subagent sessions in
  the same project count toward the budget.
- The turn limit, duration limit, or active-agent limit.
- Three consecutive short turns with no tool calls or substantive output.
- A pending permission or form.
- A model or provider error. The provider's message is shown on the binding.
- A server restart, disconnect, or explicit stop/pause.

**Goal mode** is optional. It uses the same loop but expects the agent to finish
the stated objective. With **Completion marker**, the agent can end its final
message with:

```text
GOAL_COMPLETE: short evidence
```

or ask for help with:

```text
GOAL_BLOCKED: reason
```

The marker is ignored inside fenced or indented code blocks. Without a marker,
use `/goal` or **Mark complete** when the objective is met.

## Panel commands

Type these in the panel's composer:

| Command | Effect |
| --- | --- |
| `/goal <objective>` | Set a new objective and resume the loop. |
| `/goal` | Mark the agent complete and stop. |
| `/goal pause` | Pause the loop. |
| `/goal resume` | Resume a paused loop. |
| `/goal clear` | Clear the objective and pause. |
| `/stop` | Interrupt the current turn and pause. |
| `/budget <tokens>` | Change the token cap. |
| `/status` | Show the current binding state. |

A normal chat message pauses the autonomous loop first, then queues the
message. This prevents the loop from racing your instruction. Use
`/goal resume` when you want it to continue. Project slash commands such as
`/engage.recon` are forwarded through OpenCode's command endpoint when the
project's command catalog contains them.

`/goal` typed in the OpenCode TUI is detected from the session transcript and
applies the same stop rule. The panel labels its own loop prompts as **Loop
prompt**; the messages API omits them unless `include_office=1` is passed.

## Limits and storage

| Setting | Default | Range |
| --- | --- | --- |
| Token budget | 2,000,000 | 1–100,000,000 |
| Max turns | 100 | 0–10,000 (0 disables) |
| Max duration | 8 hours | 0–168 hours (0 disables) |
| Active managed agents | 4 | 1–16 |
| Bindings | 64 | fixed |

The panel sends `X-Agent-Office-Token` on mutations and enforces the same
loopback Host, Origin, and cross-site checks as the task runner. The OpenCode
service password stays in the Python process; it is never sent to the browser.

To reset bindings, stop Agent Office and remove `agents.json`. The OpenCode
sessions themselves are retained. **Unbind** removes only the office binding.

## Troubleshooting

- **Disabled:** start with `--enable-opencode-agents` and at least one
  `--project-root`.
- **OpenCode disconnected:** start the local OpenCode service and use
  **Reconnect OpenCode**. The panel reads the service registration from
  `~/.local/state/opencode/service.json`, or from `OPENCODE_STATE_HOME` /
  `XDG_STATE_HOME` when configured.
- **Project not listed:** it must resolve inside a configured root. The panel
  also lists directories with common project markers directly under each root.
- **Agent not listed:** the project's OpenCode catalog only exposes primary
  agents with tools enabled. Hidden utility agents are excluded.
- **Waiting input:** answer the permission or form in the panel. The loop
  resumes automatically after the answer; a normal chat message still pauses it.
- **Provider error:** fix the provider, model, or quota, then press **Resume**.
  The loop retries the failed turn instead of pausing on the same error again.

## Development status

Current verification results, known blockers, and next checks are recorded in
the [implementation handoff](opencode-agents-handoff.md).
