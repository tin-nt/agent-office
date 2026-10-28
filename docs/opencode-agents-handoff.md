# Managed OpenCode agents — implementation handoff

Working notes for the next session. The user-facing contract is in
[opencode-agents.md](opencode-agents.md); this file records what is verified,
what is still blocked, and the exact next checks. Delete or fold this file into
the contract once the remaining checks pass.

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
- Real OpenCode API: session create/delete, custom IDs, direct model turn
  (`glm-5.3-flash-free` returned `OK`), and the error path. The error E2E
  confirmed that a provider error followed by `idle` pauses the agent, keeps
  `turns` at 0, and sends no continuation.

## Blocked

- **Successful real-model loop E2E.** The free OpenCode models return
  `Free usage limit reached` for the loop's initial prompt after a small number
  of probe turns. A direct single-turn probe succeeded, so the model/API path is
  good; the free-tier quota is the blocker. Retried with `flint-40b-free` after
  the quota appeared to reset; it failed the same way. Re-run when a funded model
  or fresh free quota is available.
- **Windows loopback resets.** Rapid loopback connections on this machine can
  still produce `WinError 10054` / timeouts. Keep-alive fixes the OpenCode client
  path; unrelated existing tests that open short-lived connections still hit it.
- **Leftover test directories.** Empty `_agent-office-e2e-*` directories under
  the configured project root are held by OpenCode's file watcher. They can be
  deleted after OpenCode restarts. No sessions remain.

## Next checks

1. Run the full loop E2E with an available model and confirm
   `status=complete`, `completion_evidence` set, and no extra continuation.
2. Confirm `session.tokens` (and therefore `tokens_used`) increases after a
   successful turn; the unit tests fake this.
3. Review the panel visually in a real browser. The in-app browser on this
   machine returned 502 for loopback pages; use the printed
   `http://127.0.0.1:<port>` URL.
4. Remove this handoff file after the real-model success path is confirmed.

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
