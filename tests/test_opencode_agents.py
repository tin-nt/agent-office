"""Unit boundaries for the opt-in OpenCode agent control plane."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import uuid

import pytest

from opencode_agents import (
    AgentError,
    BindingStore,
    OpenCodeAgents,
    _marker,
    _session_tokens,
)


class FakeAPI:
    def __init__(self, root: Path):
        self.root = root
        self.prompts = []
        self.command_calls = []
        self.permission_replies = []
        self.form_replies = []
        self.created_sessions = []
        self.messages_data = []
        self.active = {}
        self.pending_permissions = []
        self.pending_forms = []
        self.inbox_data = []
        self.sessions_data = []
        self.session_tokens = {
            "input": 0, "output": 0, "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        self.session_cost = 0.0
        self._counter = 0

    def _session(self, session_id="ses_test"):
        return {
            "id": session_id,
            "parentID": None,
            "location": {"directory": str(self.root)},
            "tokens": dict(self.session_tokens),
            "cost": self.session_cost,
            "time": {"created": 1, "updated": 2},
            "title": "Test",
            "agent": "build",
            "model": {"providerID": "p", "id": "m"},
        }

    def info(self):
        return {"version": "2.2.0", "urls": ["http://127.0.0.1:1"]}

    def projects(self):
        return [{"id": "p1", "canonical": str(self.root), "vcs": "git"}]

    def agents(self, directory):
        return [
            {"id": "build", "name": "Build", "mode": "primary", "hidden": False},
            {"id": "plan", "name": "Plan", "mode": "primary", "hidden": False},
        ]

    def commands(self, directory):
        return [{"name": "review", "description": "Review"}]

    def models(self):
        return [{
            "providerID": "p", "id": "m", "name": "Model M", "enabled": True,
            "type": "chat", "capabilities": {"tools": True}, "variants": [],
            "limit": {"context": 1000, "output": 100},
        }]

    def default_model(self):
        return {"providerID": "p", "id": "m"}

    def create_session(self, **kwargs):
        self._counter += 1
        session = self._session(f"ses_{self._counter}")
        session["location"] = {"directory": kwargs["directory"]}
        self.created_sessions.append(kwargs)
        return session

    def session(self, session_id):
        return self._session(session_id)

    def sessions(self, directory=None, limit=200, order="desc"):
        if self.sessions_data:
            return [dict(item) for item in self.sessions_data]
        return [self._session()]

    def active_sessions(self):
        return dict(self.active)

    def permissions(self, session_id):
        return list(self.pending_permissions)

    def forms(self, session_id):
        return list(self.pending_forms)

    def inbox(self, session_id):
        return list(self.inbox_data)

    def messages(self, session_id, **kwargs):
        return {
            "data": [dict(item) for item in self.messages_data],
            "cursor": {"next": None, "previous": None},
        }

    def prompt(self, session_id, text, metadata=None, delivery="queue", message_id=None):
        self.prompts.append({"text": text, "metadata": metadata, "delivery": delivery})
        return {"id": f"inbox_{len(self.prompts)}"}

    def command(self, session_id, name, text, delivery="queue"):
        self.command_calls.append({"name": name, "text": text, "delivery": delivery})
        return {"id": f"command_{len(self.command_calls)}"}

    def interrupt(self, session_id, resume=False):
        return {"interrupted": True}

    def reply_permission(self, session_id, request_id, decision, message=None):
        self.permission_replies.append((request_id, decision, message))
        return {}

    def reply_form(self, session_id, form_id, answer):
        self.form_replies.append((form_id, answer))
        return {}


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    (path / "README.md").write_text("test", encoding="utf-8")
    return path


@pytest.fixture
def api(project):
    return FakeAPI(project)


@pytest.fixture
def agents(project, api, tmp_path):
    manager = OpenCodeAgents(
        [project], office_dir=tmp_path / "office", api=api, interval=3600,
    )
    yield manager
    manager.shutdown(timeout=2)


def _payload(project, **updates):
    payload = {
        "request_id": str(uuid.uuid4()),
        "project_directory": str(project),
        "agent_id": "build",
        "model": {"providerID": "p", "id": "m"},
        "goal": "Finish the task",
        "mode": "continuous",
        "max_tokens": 1000,
        "max_turns": 5,
        "max_duration_s": 60,
        "start": True,
    }
    payload.update(updates)
    return payload


def _assistant(message_id, text="working", tools=None):
    content = [{"type": "text", "text": text}]
    for tool in tools or []:
        content.append({
            "type": "tool", "id": tool, "name": tool,
            "state": {"status": "completed", "input": {"x": 1}},
        })
    return {
        "id": message_id, "type": "assistant", "time": {"created": 2},
        "content": content,
    }


def _user(message_id="msg_user", text="hello"):
    return {
        "id": message_id, "type": "user", "time": {"created": 1}, "text": text,
    }


def test_create_sends_initial_prompt_and_exposes_summary(agents, api, project):
    binding = agents.create(_payload(project))
    assert binding["status"] == "running"
    assert binding["initial_sent"] is True
    assert binding["session_id"] == "ses_1"
    assert api.prompts[0]["metadata"]["agent_office"]["kind"] == "goal"
    assert "Finish the task" in api.prompts[0]["text"]
    assert api.created_sessions[0]["agent"] == "build"
    assert api.created_sessions[0]["directory"] == str(project)


def test_create_rejects_unknown_agent_and_outside_project(agents, api, project, tmp_path):
    with pytest.raises(AgentError) as exc:
        agents.create(_payload(project, agent_id="missing"))
    assert exc.value.status == 400
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(AgentError) as exc:
        agents.create(_payload(outside))
    assert exc.value.status == 403


def test_one_active_agent_per_project(agents, api, project):
    first = agents.create(_payload(project))
    with pytest.raises(AgentError) as exc:
        agents.create(_payload(project))
    assert exc.value.status == 409
    agents.goal(first["id"], {"action": "pause"})
    second = agents.create(_payload(project))
    assert second["id"] != first["id"]


def test_active_limit_is_enforced_across_projects(project, api, tmp_path):
    second = project.parent / "second"
    second.mkdir()
    manager = OpenCodeAgents(
        [project.parent], max_active=1, office_dir=tmp_path / "office", api=api,
        interval=3600,
    )
    try:
        manager.create(_payload(project))
        with pytest.raises(AgentError) as exc:
            manager.create(_payload(second))
        assert exc.value.status == 409
    finally:
        manager.shutdown(timeout=2)


def test_create_is_idempotent_and_rejects_reused_request_id(agents, api, project):
    payload = _payload(project)
    first = agents.create(payload)
    again = agents.create(payload)
    assert again["id"] == first["id"]
    assert len(api.created_sessions) == 1
    with pytest.raises(AgentError) as exc:
        agents.create({**payload, "goal": "Different goal"})
    assert exc.value.status == 409


def test_prompt_is_idempotent_and_pauses_the_loop(agents, api, project):
    binding = agents.create(_payload(project))
    request_id = str(uuid.uuid4())
    agents.prompt(binding["id"], {"text": "one", "request_id": request_id})
    count = len(api.prompts)
    agents.prompt(binding["id"], {"text": "one", "request_id": request_id})
    assert len(api.prompts) == count
    assert agents.detail(binding["id"])["status"] == "paused"
    with pytest.raises(AgentError) as exc:
        agents.prompt(binding["id"], {"text": "two", "request_id": request_id})
    assert exc.value.status == 409


def test_office_commands_control_goal_and_budget(agents, api, project):
    binding = agents.create(_payload(project))
    agents.prompt(binding["id"], {"text": "/budget 5,000"})
    assert agents.detail(binding["id"])["max_tokens"] == 5000
    agents.prompt(binding["id"], {"text": "/goal pause"})
    assert agents.detail(binding["id"])["status"] == "paused"
    agents.prompt(binding["id"], {"text": "/goal resume"})
    assert agents.detail(binding["id"])["status"] == "running"
    agents.prompt(binding["id"], {"text": "/goal"})
    assert agents.detail(binding["id"])["status"] == "complete"
    with pytest.raises(AgentError):
        agents.prompt(binding["id"], {"text": "after complete"})


def test_project_command_is_forwarded_without_office_metadata(agents, api, project):
    binding = agents.create(_payload(project))
    agents.prompt(binding["id"], {"text": "/review focus on tests"})
    assert api.command_calls[-1]["name"] == "review"
    assert api.command_calls[-1]["text"] == "focus on tests"


def test_continuation_is_sent_once_per_assistant_message(agents, api, project):
    binding = agents.create(_payload(project))
    agents._update(binding["id"], last_continuation_at=0)
    api.messages_data = [_assistant("msg_a1", "x" * 500)]
    agents._tick(api, binding["id"])
    first = len(api.prompts)
    assert first == 2
    agents._tick(api, binding["id"])
    assert len(api.prompts) == first
    agents._update(binding["id"], last_continuation_at=0)
    api.messages_data = [_assistant("msg_a2", "y" * 500)]
    agents._tick(api, binding["id"])
    assert len(api.prompts) == first + 1


def test_idle_messages_do_not_hide_the_assistant_turn(agents, api, project):
    binding = agents.create(_payload(project))
    agents._update(binding["id"], last_continuation_at=0)
    api.messages_data = [
        _assistant("msg_a1", "x" * 500),
        {"id": "msg_idle1", "type": "idle", "time": {"created": 3}},
    ]
    agents._tick(api, binding["id"])
    first = len(api.prompts)
    assert first == 2
    agents._update(binding["id"], last_continuation_at=0)
    api.messages_data = [
        _assistant("msg_a1", "x" * 500),
        {"id": "msg_idle2", "type": "idle", "time": {"created": 4}},
    ]
    agents._tick(api, binding["id"])
    assert len(api.prompts) == first


def test_assistant_error_pauses_even_when_an_idle_message_follows(agents, api, project):
    binding = agents.create(_payload(project))
    api.messages_data = [
        {
            "id": "msg_error", "type": "assistant", "time": {"created": 2},
            "content": [], "finish": "error",
            "error": "Free usage limit reached.",
        },
        {"id": "msg_idle", "type": "idle", "time": {"created": 3}},
    ]
    agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["status"] == "error"
    assert "Free usage limit" in detail["error"]
    assert len(api.prompts) == 1


def test_normalized_message_exposes_string_error_and_content_text(agents, api, project):
    binding = agents.create(_payload(project))
    api.messages_data = [{
        "id": "msg_error", "type": "assistant", "time": {"created": 2},
        "content": [{"type": "text", "text": "partial answer"}],
        "finish": "error", "error": "Free usage limit reached.",
    }]
    message = agents.messages(binding["id"], {})["messages"][0]
    assert message["role"] == "assistant"
    assert message["text"] == "partial answer"
    assert message["error"] == "Free usage limit reached."


def test_resume_retries_the_same_error_instead_of_repausing(agents, api, project):
    binding = agents.create(_payload(project))
    api.messages_data = [{
        "id": "msg_error", "type": "assistant", "time": {"created": 2},
        "content": [], "finish": "error", "error": "Free usage limit reached.",
    }]
    agents._tick(api, binding["id"])
    assert agents.detail(binding["id"])["status"] == "error"
    agents.goal(binding["id"], {"action": "resume"})
    agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["status"] == "running"
    assert len(api.prompts) == 2


def test_resume_after_an_older_error_retries_the_recorded_turn(agents, api, project):
    binding = agents.create(_payload(project))
    agents._update(
        binding["id"], status="error", pause_reason="error", error="quota",
        last_assistant_message_id="msg_old", last_error_message_id=None,
    )
    api.messages_data = [{
        "id": "msg_old", "type": "assistant", "time": {"created": 2},
        "content": [], "finish": "error", "error": "quota",
    }]
    agents.goal(binding["id"], {"action": "resume"})
    agents._tick(api, binding["id"])
    assert agents.detail(binding["id"])["status"] == "running"
    assert len(api.prompts) == 2


def test_stall_limit_pauses_repeated_short_turns(agents, api, project):
    binding = agents.create(_payload(project))
    for index in range(3):
        agents._update(binding["id"], last_continuation_at=0)
        api.messages_data = [_assistant(f"msg_a{index}", "ok")]
        agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["status"] == "paused"
    assert detail["pause_reason"] == "stalled"


def test_marker_completion_and_blocked_marker(agents, api, project):
    binding = agents.create(_payload(project, mode="goal", completion="marker"))
    api.messages_data = [_assistant("msg_a1", "done\nGOAL_COMPLETE: tests pass")]
    agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["status"] == "complete"
    assert "tests pass" in detail["completion_evidence"]

    second = agents.create(_payload(project, start=False, mode="goal", completion="marker"))
    agents.goal(second["id"], {"action": "resume"})
    api.messages_data = [_assistant("msg_a2", "GOAL_BLOCKED: need a credential")]
    agents._tick(api, second["id"])
    detail = agents.detail(second["id"])
    assert detail["status"] == "paused"
    assert "need a credential" in detail["error"]


def test_goal_typed_in_the_opencode_transcript_is_detected(agents, api, project):
    binding = agents.create(_payload(project))
    api.messages_data = [_user("msg_goal", "/goal")]
    agents._tick(api, binding["id"])
    assert agents.detail(binding["id"])["status"] == "complete"


def test_pending_permission_marks_agent_waiting_and_resumes_after_reply(agents, api, project):
    binding = agents.create(_payload(project))
    api.pending_permissions = [{
        "id": "per_1", "sessionID": binding["session_id"], "action": "bash",
        "resources": ["echo hi"],
    }]
    api.messages_data = [_assistant("msg_a1", "x" * 500)]
    agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["status"] == "waiting_input"
    assert detail["permissions"][0]["id"] == "per_1"
    api.pending_permissions = []
    agents.reply_permission(binding["id"], {"request_id": "per_1", "decision": "once"})
    assert api.permission_replies[-1][0] == "per_1"
    assert agents.detail(binding["id"])["status"] == "running"


def test_budget_uses_fresh_tokens_and_includes_descendants(agents, api, project):
    binding = agents.create(_payload(project))
    api.session_tokens = {
        "input": 100, "output": 50, "reasoning": 25,
        "cache": {"read": 1000, "write": 10},
    }
    api.session_cost = 0.1
    api.sessions_data = [
        {**api._session(binding["session_id"]), "parentID": None,
         "tokens": {"input": 100, "output": 50, "reasoning": 25,
                    "cache": {"read": 1000, "write": 10}}, "cost": 0.1},
        {**api._session("ses_child"), "parentID": binding["session_id"],
         "tokens": {"input": 200, "output": 25, "reasoning": 5,
                    "cache": {"read": 500, "write": 5}}, "cost": 0.2},
    ]
    agents._tick(api, binding["id"])
    detail = agents.detail(binding["id"])
    assert detail["tokens_used"] == 420
    assert detail["tokens_total"] == 1920
    assert detail["cost_usd"] == 0.3


def test_restart_pauses_active_bindings(project, api, tmp_path):
    office_dir = tmp_path / "office"
    first = OpenCodeAgents([project.parent], office_dir=office_dir, api=api, interval=3600)
    binding = first.create(_payload(project))
    assert binding["status"] == "running"
    first.shutdown(timeout=2)

    second = OpenCodeAgents([project.parent], office_dir=office_dir, api=api, interval=3600)
    second.start()
    try:
        detail = second.detail(binding["id"])
        assert detail["status"] == "paused"
        assert detail["pause_reason"] == "restart"
    finally:
        second.shutdown(timeout=2)


def test_binding_store_preserves_unknown_files(project, api, tmp_path):
    path = tmp_path / "agents.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AgentError) as exc:
        BindingStore(path)
    assert exc.value.status == 503
    assert path.read_text(encoding="utf-8") == "{not json"


def test_validation_rejects_extra_fields_and_bad_limits(agents, project):
    with pytest.raises(AgentError):
        agents.create({**_payload(project), "unexpected": True})
    with pytest.raises(AgentError):
        agents.create(_payload(project, max_tokens=0))
    with pytest.raises(AgentError):
        agents.create(_payload(project, mode="unknown"))
    with pytest.raises(AgentError):
        agents.create(_payload(project, model={"providerID": "p"}))


def test_session_tokens_excludes_cache_reads():
    fresh, total, reported = _session_tokens({
        "tokens": {"input": 10, "output": 5, "reasoning": 2,
                   "cache": {"read": 100, "write": 3}},
    })
    assert (fresh, total, reported) == (20, 120, True)


def test_marker_ignores_fenced_code():
    assert _marker("```\nGOAL_COMPLETE: no\n```") is None
    assert _marker("text\nGOAL_COMPLETE: yes")["kind"] == "complete"
    assert _marker("    GOAL_COMPLETE: indented") is None
