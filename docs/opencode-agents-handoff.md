# Managed OpenCode agents — implementation handoff

Working notes for the next session. The user-facing contract is in
[opencode-agents.md](opencode-agents.md); this file records what is verified,
what remains, and the exact next checks. Delete or fold this file into the
contract once the remaining checks pass.

## State

The opt-in control plane is implemented and wired into the panel:

- `opencode_api.py` — loopback-only OpenCode V2 client, service discovery,
  Basic auth, retries, and thread-local keep-alive connections.
- `opencode_agents.py` — project/session bindings, continuous loop, budget
  accounting, stop rules, permission/form replies, TUI `/goal` detection.
- `__init__.py` — `/agents` routes and the loopback Host/Origin/token boundary.
- `run.py` — `--enable-opencode-agents`, `--project-root`,
  `--max-active-agents`.
- `web/js/agents.js`, `web/css/agents.css`, `web/template.html`,
  `web/js/office.js` — roster, transcript, composer, goal/budget controls,
  permission/form cards, new-agent form.
- `tests/test_opencode_agents.py` — 24 unit tests.

Behavior that changed during the last pass:

- The loop now derives the latest **assistant** message instead of the latest
  message of any type. A trailing `idle` event no longer hides an assistant
  error, marker, stall decision, or continuation dedupe.
- Assistant errors may be strings or objects; both are surfaced on the binding
  and in the normalized transcript.
- After an error, **Resume** retries the failed turn once instead of
  immediately re-pausing on the same assistant message.
- The transcript hides `idle`, compaction, model-selection, and other runtime
  events; only user and assistant turns are rendered.
- Form fields with `when` conditions (`eq`/`neq`) show and hide as answers
  change; hidden fields are omitted from the submitted answer so a required
  field cannot block a branch that is not active.
- The static public preview removes the Agent chat entry points, because it has
  no local control plane.
- The OpenCode transport reuses HTTP/1.1 connections, which avoids most
  Windows/Python `WinError 10054` resets during polling.

## Verified

- `python -m pytest tests/test_opencode_agents.py -q` — 24 passed.
- Fake-API HTTP smoke (`agents_http_smoke.py`) — create, detail, messages,
  prompt, pause/resume, permission, form, catalog, static assets, unbind.
- `tests/test_preview_build.py` — passed; the preview build still rewrites the
  template correctly.
- jsdom checks: roster, detail, transcript, tools, conditional forms,
  permissions, composer, new-agent form, goal editor, and the preview guard.
- Real OpenCode API: session create/delete, custom IDs, direct model turns,
  and both loop outcomes.
  - **Success:** `status=complete`, `completion_evidence="replied OK"`, `turns=0`,
    and `tokens_used=16,882` for a turn reporting 16,870 input + 12 output.
    The 1,920 cache-read tokens were excluded, matching the documented budget
    metric.
  - **Error:** a provider error followed by `idle` pauses the agent, keeps
    `turns` at 0, and sends no continuation.
  - The successful loop used the configured `ckey/deepseek-4.1-flash-ckey`
    provider after the free models exhausted their quota.

## Remaining

- **Visual review.** The in-app browser on this machine returned 502 for
  loopback pages. The panel is covered by jsdom checks, but it still needs a
  visual pass in a real browser at the printed `http://127.0.0.1:<port>` URL.
- **Windows loopback resets.** Rapid loopback connections on this machine can
  still produce `WinError 10054` / timeouts. Keep-alive fixes the OpenCode client
  path; unrelated existing tests that open short-lived connections still hit it.
- **Leftover test directories.** Empty `_agent-office-e2e-*` directories under
  the configured project root can be held by OpenCode's file watcher until it
  releases them. No sessions remain.

## Next checks

1. Open the panel in a real browser and confirm the roster, transcript, goal
   editor, forms, and composer layout at desktop and narrow widths.
2. Run a real-model goal that needs at least two turns, to confirm continuation
   prompts, turn counting, and `/goal` stop behavior outside the unit tests.
3. Remove this handoff file after those checks pass.

## Run

```sh
py -3 run.py --enable-opencode-agents --project-root "D:\LLM\Projects"
```

Open the printed `http://127.0.0.1:<port>` URL (the control routes require the
exact `127.0.0.1` Host), then **Agent chat**. Defaults: continuous mode,
2,000,000 tokens, 100 turns, 8 hours, 4 active agents.

The working tree also contains pre-existing user changes under `opencode/`,
`install.py`, `docs/runtime-observers.md`, and related tests. Do not overwrite or
commit those as part of this feature.
