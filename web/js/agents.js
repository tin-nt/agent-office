/* Opt-in OpenCode agent control plane. The observer stays observation-only. */
(function () {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const root = byId("sheet-agents");
  if (!root) return;

  // The public static demo has no local OpenCode control plane.  Hide the
  // entry points instead of advertising a panel that cannot connect.
  if (document.body?.dataset.agentOfficePreview === "1") {
    byId("agentsbtn")?.remove();
    root.remove();
    // office.js binds the inspector action directly, so remove that entry
    // point only after every script has finished binding.
    document.addEventListener(
      "DOMContentLoaded",
      () => byId("inspector-agent-chat")?.remove(),
      { once: true },
    );
    return;
  }

  const els = {
    enable: byId("agents-enable"),
    refresh: byId("agents-refresh"),
    reconnect: byId("agents-reconnect"),
    newButton: byId("agents-new"),
    status: byId("agents-status"),
    list: byId("agents-list"),
    listEmpty: byId("agents-list-empty"),
    empty: byId("agents-empty"),
    view: byId("agents-view"),
    title: byId("agents-title"),
    meta: byId("agents-meta"),
    state: byId("agents-state"),
    error: byId("agents-error"),
    pause: byId("agents-pause"),
    resume: byId("agents-resume"),
    stop: byId("agents-stop"),
    complete: byId("agents-complete"),
    unbind: byId("agents-unbind"),
    tokens: byId("agents-tokens"),
    budget: byId("agents-budget"),
    turns: byId("agents-turns"),
    elapsed: byId("agents-elapsed"),
    goal: byId("agents-goal-text"),
    mode: byId("agents-mode"),
    completion: byId("agents-completion"),
    goalSave: byId("agents-goal-save"),
    pending: byId("agents-pending"),
    permissions: byId("agents-permissions"),
    forms: byId("agents-forms"),
    transcript: byId("agents-transcript"),
    chatStatus: byId("agents-chat-status"),
    compose: byId("agents-compose"),
    prompt: byId("agents-prompt"),
    command: byId("agents-command"),
    send: byId("agents-send"),
    composeStatus: byId("agents-compose-status"),
    newForm: byId("agents-new-form"),
    newProject: byId("agents-new-project"),
    newAgent: byId("agents-new-agent"),
    newModel: byId("agents-new-model"),
    newTitle: byId("agents-new-title"),
    newGoal: byId("agents-new-goal"),
    newMode: byId("agents-new-mode"),
    newCompletion: byId("agents-new-completion"),
    newTokens: byId("agents-new-tokens"),
    newTurns: byId("agents-new-turns"),
    newHours: byId("agents-new-hours"),
    newStart: byId("agents-new-start"),
    newSubmit: byId("agents-new-submit"),
    newCancel: byId("agents-new-cancel"),
    newStatus: byId("agents-new-status"),
  };

  const STATUS_LABELS = {
    starting: "Starting",
    running: "Running",
    waiting_input: "Needs input",
    paused: "Paused",
    complete: "Complete",
    error: "Error",
    budget_exhausted: "Budget spent",
    limit_reached: "Limit reached",
    stopped: "Stopped",
  };
  const PAUSE_REASONS = {
    user: "paused by you",
    restart: "paused after Agent Office restarted",
    disconnected: "paused because OpenCode disconnected",
    input: "waiting for a permission or form answer",
    budget: "token budget reached",
    limit: "turn or time limit reached",
    stalled: "no progress across repeated turns",
    error: "paused after an error",
  };
  const ACTIVE_STATUSES = new Set(["starting", "running", "waiting_input"]);

  let active = false;
  let capability = null;
  let token = "";
  let bindings = [];
  let selectedId = null;
  let selected = null;
  let messages = [];
  let catalog = null;
  let catalogProject = "";
  let catalogPromise = null;
  let statusTimer = null;
  let detailTimer = null;
  let statusInFlight = false;
  let detailInFlight = false;
  let rosterSignature = "";
  let transcriptSignature = "";
  let pendingSignature = "";
  let permissionsInFlight = false;
  let formsInFlight = false;
  let sending = false;
  let creating = false;
  let goalDirty = false;
  let goalBindingId = null;
  let transcriptAtBottom = true;
  let composeBindingId = null;
  let commandSignature = "";
  const catalogCache = new Map();

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  function setStatus(text, isError) {
    setText(els.status, text || "");
    els.status.dataset.error = isError ? "1" : "";
  }

  function setComposeStatus(text, isError) {
    setText(els.composeStatus, text || "");
    els.composeStatus.dataset.error = isError ? "1" : "";
  }

  function setNewStatus(text, isError) {
    setText(els.newStatus, text || "");
    els.newStatus.dataset.error = isError ? "1" : "";
  }

  function compact(number) {
    const value = Number(number) || 0;
    if (value >= 1e9) return (value / 1e9).toFixed(1) + "B";
    if (value >= 1e6) return (value / 1e6).toFixed(2) + "M";
    if (value >= 1e3) return (value / 1e3).toFixed(1) + "k";
    return String(Math.round(value));
  }

  function duration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    if (value < 60) return value + "s";
    if (value < 3600) return Math.floor(value / 60) + "m " + (value % 60) + "s";
    return (
      Math.floor(value / 3600) +
      "h " +
      Math.floor((value % 3600) / 60) +
      "m"
    );
  }

  function timestamp(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "";
    const date = new Date(value * 1000);
    return Number.isFinite(date.getTime()) ? date.toLocaleTimeString() : "";
  }

  function statusLabel(binding) {
    const label = STATUS_LABELS[binding?.status] || binding?.status || "Unknown";
    if (binding?.pause_reason && binding.status === "paused") {
      const reason = PAUSE_REASONS[binding.pause_reason];
      return reason ? label + " · " + reason : label;
    }
    return label;
  }

  function randomId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0"));
    return (
      hex.slice(0, 4).join("") +
      "-" +
      hex.slice(4, 6).join("") +
      "-" +
      hex.slice(6, 8).join("") +
      "-" +
      hex.slice(8, 10).join("") +
      "-" +
      hex.slice(10, 16).join("")
    );
  }

  async function request(path, options = {}) {
    const headers = { Accept: "application/json" };
    const init = { method: options.method || "GET", headers, cache: "no-store" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (init.method !== "GET") {
      if (!token) throw new Error("Agent control token is not available.");
      headers["X-Agent-Office-Token"] = token;
    }
    const response = await fetch(path, init);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const message =
        payload && typeof payload.error === "string"
          ? payload.error
          : "Agent control request failed (HTTP " + response.status + ").";
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function post(path, body) {
    return request(path, { method: "POST", body: body || {} });
  }

  function bindingById(id) {
    return bindings.find((item) => item.id === id) || null;
  }

  function projectFor(binding) {
    return binding ? binding.project_directory : "";
  }

  function setSelected(id) {
    if (selectedId === id) return;
    selectedId = id;
    selected = bindingById(id);
    messages = [];
    transcriptSignature = "";
    pendingSignature = "";
    goalDirty = false;
    goalBindingId = null;
    composeBindingId = null;
    setComposeStatus("");
    renderRoster();
    renderDetail();
    if (id) {
      loadDetail();
      loadCatalog(projectFor(selected)).catch(() => {});
    }
  }

  function renderRoster() {
    const signature = JSON.stringify([
      selectedId,
      bindings.map((binding) => [
        binding.id,
        binding.title,
        binding.project_directory,
        binding.agent_id,
        binding.status,
        binding.pause_reason,
        binding.tokens_used,
        binding.max_tokens,
        binding.turns,
      ]),
    ]);
    if (signature === rosterSignature) {
      els.listEmpty.hidden = bindings.length > 0;
      return;
    }
    rosterSignature = signature;
    els.list.replaceChildren();
    for (const binding of bindings) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "agents-card";
      button.dataset.agentId = binding.id;
      button.setAttribute("aria-pressed", String(binding.id === selectedId));
      const head = document.createElement("strong");
      const name = document.createElement("span");
      name.textContent = binding.title || binding.project_name || binding.id;
      const state = document.createElement("span");
      state.className = "agents-state";
      state.dataset.state = binding.status || "paused";
      state.textContent = statusLabel(binding);
      head.append(name, state);
      const project = document.createElement("small");
      project.textContent = binding.project_directory || "";
      const usage = document.createElement("small");
      usage.textContent =
        (binding.agent_id || "agent") +
        " · " +
        compact(binding.tokens_used) +
        " / " +
        compact(binding.max_tokens) +
        " tokens" +
        (binding.turns ? " · " + binding.turns + " turns" : "");
      button.append(head, project, usage);
      button.addEventListener("click", () => setSelected(binding.id));
      els.list.append(button);
    }
    els.listEmpty.hidden = bindings.length > 0;
    if (!bindings.length) {
      els.empty.hidden = false;
      els.view.hidden = true;
    }
  }

  function renderDetail() {
    const binding = selected || bindingById(selectedId);
    selected = binding;
    if (!binding) {
      els.empty.hidden = false;
      els.view.hidden = true;
      setText(els.title, "");
      setText(els.meta, "");
      setText(els.state, "");
      els.state.dataset.state = "";
      els.error.hidden = true;
      return;
    }
    els.empty.hidden = true;
    els.view.hidden = false;
    setText(els.title, binding.title || binding.project_name || binding.id);
    setText(
      els.meta,
      [
        binding.project_directory,
        binding.agent_id,
        binding.model
          ? (binding.model.providerID || "") + "/" + (binding.model.id || "")
          : "",
      ]
        .filter(Boolean)
        .join(" · "),
    );
    setText(els.state, statusLabel(binding));
    els.state.dataset.state = binding.status || "";
    if (binding.error) {
      setText(els.error, binding.error);
      els.error.hidden = false;
    } else {
      els.error.hidden = true;
    }
    const used = Number(binding.tokens_used) || 0;
    const max = Math.max(1, Number(binding.max_tokens) || 1);
    setText(
      els.tokens,
      compact(used) +
        " / " +
        compact(max) +
        " (" +
        Math.min(100, Math.round((used / max) * 100)) +
        "%)",
    );
    els.budget.max = max;
    els.budget.value = Math.min(max, used);
    setText(
      els.turns,
      "Turns " +
        (binding.turns || 0) +
        (binding.max_turns ? " / " + binding.max_turns : ""),
    );
    const started = binding.started_at || binding.created_at;
    setText(
      els.elapsed,
      started
        ? "Elapsed " + duration(Date.now() / 1000 - Number(started))
        : "Not started",
    );
    if (goalBindingId !== binding.id || !goalDirty) {
      if (document.activeElement !== els.goal) els.goal.value = binding.goal || "";
      els.mode.value = binding.mode || "continuous";
      els.completion.value = binding.completion || "manual";
      goalBindingId = binding.id;
    }
    els.pause.disabled = !ACTIVE_STATUSES.has(binding.status);
    els.resume.disabled =
      binding.status === "complete" || ACTIVE_STATUSES.has(binding.status);
    els.stop.disabled = !ACTIVE_STATUSES.has(binding.status);
    els.complete.disabled = binding.status === "complete";
    els.unbind.disabled = false;
    els.send.disabled =
      sending ||
      ["complete", "budget_exhausted", "limit_reached", "error"].includes(
        binding.status,
      );
    renderPending(binding);
    renderCommands(binding);
    renderTranscript();
  }

  function renderPending(binding) {
    const permissions = Array.isArray(binding.permissions) ? binding.permissions : [];
    const forms = Array.isArray(binding.forms) ? binding.forms : [];
    const signature = JSON.stringify([permissions, forms]);
    if (signature === pendingSignature) return;
    pendingSignature = signature;
    els.permissions.replaceChildren();
    els.forms.replaceChildren();
    els.pending.hidden = permissions.length + forms.length === 0;

    for (const permission of permissions) {
      const card = document.createElement("div");
      card.className = "agents-permission";
      const title = document.createElement("p");
      const strong = document.createElement("strong");
      strong.textContent = permission.action || "Permission request";
      title.append(strong);
      card.append(title);
      if (permission.message) {
        const message = document.createElement("p");
        message.textContent = permission.message;
        card.append(message);
      }
      const resources = Array.isArray(permission.resources)
        ? permission.resources
        : [];
      if (resources.length) {
        const list = document.createElement("ul");
        for (const resource of resources) {
          const item = document.createElement("li");
          item.textContent = resource;
          list.append(item);
        }
        card.append(list);
      }
      const actions = document.createElement("div");
      actions.className = "agents-actions";
      const choices = [
        ["once", "Allow once"],
        ["always", "Always allow"],
        ["reject", "Reject"],
      ];
      for (const [decision, label] of choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn";
        button.textContent = label;
        button.disabled = permissionsInFlight;
        button.addEventListener("click", () =>
          answerPermission(permission.id, decision),
        );
        actions.append(button);
      }
      card.append(actions);
      els.permissions.append(card);
    }

    for (const form of forms) {
      els.forms.append(renderForm(form));
    }
  }

  function fieldValue(field, control) {
    if (field.type === "external") return undefined;
    if (field.type === "boolean") return !!control.checked;
    if (field.type === "multiselect") {
      return [...control.querySelectorAll("input:checked")].map(
        (input) => input.value,
      );
    }
    if (field.type === "integer" || field.type === "number") {
      if (control.value === "") return undefined;
      const value = Number(control.value);
      return Number.isFinite(value) ? value : control.value;
    }
    return control.value;
  }

  function conditionMatches(condition, controls) {
    if (!condition || typeof condition.key !== "string") return true;
    const entry = controls.find((item) => item.field.key === condition.key);
    if (!entry) return false;
    const actual = fieldValue(entry.field, entry.control);
    const wanted = condition.value;
    let equal;
    if (Array.isArray(actual)) equal = actual.includes(wanted);
    else if (actual === undefined || actual === null) equal = false;
    else equal = actual === wanted || String(actual) === String(wanted);
    return condition.op === "neq" ? !equal : equal;
  }

  function whenMatches(field, controls) {
    const conditions = Array.isArray(field.when)
      ? field.when
      : field.when
        ? [field.when]
        : [];
    return conditions.every((condition) =>
      conditionMatches(condition, controls),
    );
  }

  function syncConditionals(controls) {
    for (const entry of controls) {
      entry.wrap.hidden = !whenMatches(entry.field, controls);
    }
  }

  function renderForm(form) {
    const card = document.createElement("div");
    card.className = "agents-form";
    const heading = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = form.title || "Agent form";
    heading.append(strong);
    card.append(heading);
    const fields = document.createElement("div");
    fields.className = "agents-form-fields";
    const controls = [];
    for (const field of Array.isArray(form.fields) ? form.fields : []) {
      if (!field || typeof field.key !== "string") continue;
      if (field.hidden === true) continue;
      const wrap = document.createElement("div");
      wrap.className = "agents-form-field";
      const label = document.createElement("label");
      label.textContent = field.title || field.key;
      const control = createFieldControl(field);
      if (control) {
        const id = "agent-form-" + form.id + "-" + field.key;
        control.id = id;
        label.htmlFor = id;
        controls.push({ field, control, wrap });
        const sync = () => syncConditionals(controls);
        control.addEventListener("input", sync);
        control.addEventListener("change", sync);
        wrap.append(label);
        if (field.description) {
          const description = document.createElement("p");
          description.className = "h";
          description.textContent = field.description;
          wrap.append(description);
        }
        wrap.append(control);
      } else {
        wrap.append(label);
        if (field.description) {
          const description = document.createElement("p");
          description.className = "h";
          description.textContent = field.description;
          wrap.append(description);
        }
      }
      fields.append(wrap);
    }
    syncConditionals(controls);
    card.append(fields);
    const actions = document.createElement("div");
    actions.className = "agents-actions";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "btn";
    submit.textContent = "Submit answer";
    submit.disabled = formsInFlight;
    const status = document.createElement("p");
    status.className = "h";
    status.setAttribute("role", "status");
    submit.addEventListener("click", () =>
      submitForm(form, controls, status, submit),
    );
    actions.append(submit);
    card.append(actions, status);
    return card;
  }

  function createFieldControl(field) {
    const type = field.type;
    if (type === "external") {
      const link = document.createElement("a");
      link.href = typeof field.url === "string" ? field.url : "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = field.title || field.url || "Open external form";
      return link;
    }
    if (type === "boolean") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = field.default === true;
      return input;
    }
    if (type === "multiselect") {
      const wrap = document.createElement("div");
      wrap.className = "agents-form-multiselect";
      const options = Array.isArray(field.options) ? field.options : [];
      const defaults = Array.isArray(field.default) ? field.default : [];
      for (const option of options) {
        if (!option || typeof option.value !== "string") continue;
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = option.value;
        input.checked = defaults.includes(option.value);
        label.append(input, document.createTextNode(option.label || option.value));
        wrap.append(label);
      }
      return wrap;
    }
    if (type === "string" && Array.isArray(field.options) && field.options.length) {
      const select = document.createElement("select");
      select.className = "txt";
      for (const option of field.options) {
        if (!option || typeof option.value !== "string") continue;
        select.append(new Option(option.label || option.value, option.value));
      }
      if (typeof field.default === "string") select.value = field.default;
      return select;
    }
    const input = document.createElement("input");
    input.className = "txt";
    input.type =
      type === "number" || type === "integer" ? "number" : "text";
    if (type === "integer") input.step = "1";
    if (type === "number") input.step = "any";
    if (field.minimum !== undefined && typeof field.minimum === "number")
      input.min = String(field.minimum);
    if (field.maximum !== undefined && typeof field.maximum === "number")
      input.max = String(field.maximum);
    if (typeof field.placeholder === "string") input.placeholder = field.placeholder;
    if (typeof field.default === "string" || typeof field.default === "number")
      input.value = String(field.default);
    return input;
  }

  async function submitForm(form, controls, status, button) {
    if (formsInFlight) return;
    const answer = {};
    syncConditionals(controls);
    for (const { field, control, wrap } of controls) {
      if (field.type === "external") continue;
      if (wrap.hidden) continue;
      if (field.type === "boolean") {
        answer[field.key] = !!control.checked;
      } else if (field.type === "multiselect") {
        answer[field.key] = [...control.querySelectorAll("input:checked")].map(
          (input) => input.value,
        );
      } else if (control.value !== "") {
        if (field.type === "integer") answer[field.key] = parseInt(control.value, 10);
        else if (field.type === "number") answer[field.key] = Number(control.value);
        else answer[field.key] = control.value;
      }
      if (field.required && !(field.key in answer)) {
        setText(status, "Required: " + (field.title || field.key), true);
        return;
      }
    }
    formsInFlight = true;
    button.disabled = true;
    setText(status, "Submitting…");
    try {
      await post("/agents/" + encodeURIComponent(selectedId) + "/form", {
        form_id: form.id,
        answer,
      });
      setText(status, "Answer submitted.");
      pendingSignature = "";
      await loadDetail();
    } catch (error) {
      setText(status, error.message, true);
    } finally {
      formsInFlight = false;
      button.disabled = false;
    }
  }

  async function answerPermission(requestId, decision) {
    if (permissionsInFlight) return;
    permissionsInFlight = true;
    for (const button of els.permissions.querySelectorAll("button")) {
      button.disabled = true;
    }
    try {
      await post("/agents/" + encodeURIComponent(selectedId) + "/permission", {
        request_id: requestId,
        decision,
      });
      pendingSignature = "";
      await loadDetail();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      permissionsInFlight = false;
      renderPending(selected || {});
    }
  }

  function renderTranscript() {
    const signature = JSON.stringify(messages);
    if (signature === transcriptSignature) return;
    transcriptSignature = signature;
    const nearBottom =
      els.transcript.scrollTop + els.transcript.clientHeight >=
      els.transcript.scrollHeight - 24;
    els.transcript.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement("p");
      empty.className = "h";
      empty.textContent = "No messages yet.";
      els.transcript.append(empty);
      return;
    }
    const ordered = [...messages].sort((a, b) => {
      const left = Number(a?.time?.created) || 0;
      const right = Number(b?.time?.created) || 0;
      return left - right;
    });
    for (const message of ordered) {
      if (!message || typeof message !== "object") continue;
      // Idle, compaction, model-selection and other runtime events are not
      // conversation turns; keep the chat to user and assistant messages.
      if (message.role !== "user" && message.role !== "assistant") continue;
      const article = document.createElement("article");
      const role = message.office ? "office" : message.role;
      article.className = "agents-message";
      article.dataset.role = role;
      const meta = document.createElement("div");
      meta.className = "agents-message-meta";
      const who = document.createElement("span");
      who.textContent =
        role === "office"
          ? "Loop prompt"
          : role === "user"
            ? "You"
            : role === "assistant"
              ? "Agent"
              : message.role || "Event";
      const when = document.createElement("span");
      when.textContent = timestamp(message.time?.created);
      meta.append(who, when);
      article.append(meta);
      const text = document.createElement("p");
      text.className = "agents-message-text";
      text.textContent = message.text || "(no text)";
      article.append(text);
      if (message.reasoning) {
        const details = document.createElement("details");
        details.className = "agents-reasoning";
        const summary = document.createElement("summary");
        summary.textContent = "Reasoning";
        const pre = document.createElement("pre");
        pre.textContent = message.reasoning;
        details.append(summary, pre);
        article.append(details);
      }
      const tools = Array.isArray(message.tools) ? message.tools : [];
      if (tools.length) {
        const list = document.createElement("div");
        list.className = "agents-tools";
        for (const tool of tools) {
          const details = document.createElement("details");
          details.className = "agents-tool";
          const summary = document.createElement("summary");
          summary.textContent =
            (tool.name || "tool") + (tool.status ? " · " + tool.status : "");
          details.append(summary);
          const body = [];
          if (tool.input) body.push("Input:\n" + tool.input);
          if (tool.output) body.push("Output:\n" + tool.output);
          if (tool.error) body.push("Error:\n" + tool.error);
          if (body.length) {
            const pre = document.createElement("pre");
            pre.textContent = body.join("\n\n");
            details.append(pre);
          }
          list.append(details);
        }
        article.append(list);
      }
      if (message.error) {
        const error = document.createElement("p");
        error.className = "agents-error";
        error.textContent = message.error;
        article.append(error);
      }
      els.transcript.append(article);
    }
    if (nearBottom || transcriptAtBottom) {
      els.transcript.scrollTop = els.transcript.scrollHeight;
      transcriptAtBottom = true;
    }
  }

  function renderCommands(binding) {
    const project = projectFor(binding);
    const key = project + "|" + (catalog && catalogProject === project ? "1" : "0");
    if (key === commandSignature) return;
    commandSignature = key;
    els.command.replaceChildren(new Option("Insert project command…", ""));
    const commands =
      catalog && catalogProject === project && Array.isArray(catalog.commands)
        ? catalog.commands
        : [];
    for (const command of commands) {
      if (!command || typeof command.name !== "string") continue;
      const label = "/" + command.name;
      const option = new Option(
        command.description ? label + " — " + command.description : label,
        command.name,
      );
      els.command.append(option);
    }
    els.command.disabled = commands.length === 0;
    if (composeBindingId !== (binding ? binding.id : null)) {
      composeBindingId = binding ? binding.id : null;
      els.prompt.value = "";
    }
  }

  async function loadStatus() {
    if (statusInFlight) return;
    statusInFlight = true;
    try {
      const data = await request("/agents");
      if (!data || typeof data !== "object") {
        setStatus("Managed agents are unavailable in this build.", true);
        return;
      }
      capability = data;
      token = typeof data.token === "string" ? data.token : "";
      const enabled = data.enabled === true;
      els.enable.hidden = enabled;
      els.refresh.disabled = !enabled;
      els.reconnect.disabled = !enabled;
      els.newButton.disabled = !enabled;
      if (!enabled) {
        bindings = [];
        rosterSignature = "";
        renderRoster();
        setStatus("Managed agents are disabled on this server.", false);
        return;
      }
      bindings = Array.isArray(data.bindings) ? data.bindings : [];
      if (selectedId && !bindingById(selectedId)) {
        selectedId = null;
        selected = null;
      }
      renderRoster();
      if (selectedId) {
        const current = bindingById(selectedId);
        if (current) selected = selected ? { ...selected, ...current } : current;
      }
      renderDetail();
      const connection = data.connected
        ? "Connected to OpenCode" +
          (data.server?.version ? " " + data.server.version : "") +
          "."
        : data.error || "OpenCode is not connected.";
      setStatus(connection, !data.connected);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      statusInFlight = false;
    }
  }

  async function loadDetail() {
    if (!selectedId || detailInFlight) return;
    detailInFlight = true;
    const id = selectedId;
    try {
      const [detail, history] = await Promise.all([
        request("/agents/" + encodeURIComponent(id)),
        request(
          "/agents/" +
            encodeURIComponent(id) +
            "/messages?limit=80&order=desc&include_office=1",
        ),
      ]);
      if (selectedId !== id) return;
      selected = detail;
      bindings = bindings.map((item) => (item.id === id ? { ...item, ...detail } : item));
      messages = Array.isArray(history?.messages) ? history.messages : [];
      renderRoster();
      renderDetail();
      const pending =
        (Array.isArray(detail.permissions) ? detail.permissions.length : 0) +
        (Array.isArray(detail.forms) ? detail.forms.length : 0);
      setText(
        els.chatStatus,
        pending
          ? "The loop is waiting for " + pending + " answer(s)."
          : "Updates every few seconds while this panel is open.",
      );
    } catch (error) {
      if (error.status === 404) {
        setSelected(null);
      }
      setStatus(error.message, true);
    } finally {
      detailInFlight = false;
    }
  }

  async function loadCatalog(project) {
    if (!project) return null;
    if (catalogCache.has(project)) {
      catalog = catalogCache.get(project);
      catalogProject = project;
      renderCommands(selected);
      return catalog;
    }
    if (catalogPromise && catalogProject === project) return catalogPromise;
    catalogProject = project;
    catalogPromise = request("/agents/catalog?project=" + encodeURIComponent(project))
      .then((data) => {
        catalogCache.set(project, data);
        catalog = data;
        catalogProject = project;
        renderCommands(selected);
        return data;
      })
      .catch((error) => {
        setStatus(error.message, true);
        return null;
      })
      .finally(() => {
        catalogPromise = null;
      });
    return catalogPromise;
  }

  function schedule() {
    clearTimeout(statusTimer);
    clearTimeout(detailTimer);
    if (!active) return;
    statusTimer = setTimeout(async () => {
      await loadStatus();
      if (selectedId) await loadDetail();
      schedule();
    }, 2500);
  }

  function open() {
    active = true;
    root.classList.add("agents-active");
    if (!capability) setStatus("Loading managed agents…");
    loadStatus().then(() => {
      if (selectedId) loadDetail();
    });
    schedule();
  }

  function close() {
    active = false;
    root.classList.remove("agents-active");
    clearTimeout(statusTimer);
    clearTimeout(detailTimer);
  }

  async function control(action, body, confirmation) {
    if (!selectedId) return;
    if (confirmation && !window.confirm(confirmation)) return;
    setStatus("");
    try {
      if (action === "interrupt") {
        await post("/agents/" + encodeURIComponent(selectedId) + "/interrupt", {});
      } else if (action === "unbind") {
        await post("/agents/" + encodeURIComponent(selectedId) + "/unbind", {});
        setSelected(null);
        await loadStatus();
        return;
      } else {
        await post("/agents/" + encodeURIComponent(selectedId) + "/goal", body);
      }
      await loadDetail();
      await loadStatus();
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  function toggleNewForm(show) {
    const visible = show === undefined ? els.newForm.hidden : show;
    els.newForm.hidden = !visible;
    els.newButton.setAttribute("aria-expanded", String(visible));
    if (visible) {
      populateNewForm();
      els.newGoal.focus({ preventScroll: true });
    }
  }

  function populateNewForm() {
    const projects = Array.isArray(capability?.projects) ? capability.projects : [];
    els.newProject.replaceChildren();
    if (!projects.length) {
      els.newProject.append(new Option("No allowlisted projects found", ""));
      setNewStatus(
        "No projects are available under the configured --project-root paths.",
        true,
      );
      return;
    }
    for (const project of projects) {
      if (!project || typeof project.path !== "string") continue;
      els.newProject.append(
        new Option(project.name || project.path, project.path),
      );
    }
    setNewStatus("");
    loadNewCatalog();
  }

  async function loadNewCatalog() {
    const project = els.newProject.value;
    if (!project) return;
    els.newAgent.replaceChildren(new Option("Loading agents…", ""));
    els.newModel.replaceChildren(new Option("Loading models…", ""));
    try {
      const data = await loadCatalog(project);
      if (!data) return;
      els.newAgent.replaceChildren();
      const agents = Array.isArray(data.agents) ? data.agents : [];
      for (const agent of agents) {
        if (!agent || typeof agent.id !== "string") continue;
        els.newAgent.append(
          new Option(
            agent.description ? agent.id + " — " + agent.description : agent.id,
            agent.id,
          ),
        );
      }
      if (!agents.length) els.newAgent.append(new Option("No primary agents", ""));
      els.newModel.replaceChildren();
      const models = Array.isArray(data.models) ? data.models : [];
      for (const model of models) {
        if (!model || typeof model.providerID !== "string") continue;
        els.newModel.append(
          new Option(
            (model.name || model.id) + " (" + model.providerID + ")",
            model.providerID + "/" + model.id,
          ),
        );
      }
      if (!models.length) els.newModel.append(new Option("No tool-capable models", ""));
      if (data.default_model) {
        const value = data.default_model.providerID + "/" + data.default_model.id;
        if ([...els.newModel.options].some((option) => option.value === value))
          els.newModel.value = value;
      }
    } catch (error) {
      setNewStatus(error.message, true);
    }
  }

  async function createAgent(event) {
    event.preventDefault();
    if (creating) return;
    const project = els.newProject.value;
    const agent = els.newAgent.value;
    const model = els.newModel.value;
    const goal = els.newGoal.value.trim();
    if (!project || !agent || !model || !goal) {
      setNewStatus("Project, agent, model, and objective are required.", true);
      return;
    }
    const slash = model.indexOf("/");
    if (slash <= 0) {
      setNewStatus("Select a valid model.", true);
      return;
    }
    const maxTokens = parseInt(els.newTokens.value, 10);
    const maxTurns = parseInt(els.newTurns.value, 10);
    const maxHours = Number(els.newHours.value);
    if (!Number.isFinite(maxTokens) || maxTokens < 1) {
      setNewStatus("Token budget must be a positive integer.", true);
      return;
    }
    if (!Number.isFinite(maxTurns) || maxTurns < 0) {
      setNewStatus("Max turns must be zero or a positive integer.", true);
      return;
    }
    if (!Number.isFinite(maxHours) || maxHours < 0) {
      setNewStatus("Max hours must be zero or positive.", true);
      return;
    }
    creating = true;
    els.newSubmit.disabled = true;
    setNewStatus("Creating the OpenCode session…");
    const title = els.newTitle.value.trim();
    try {
      const binding = await post("/agents", {
        request_id: randomId(),
        project_directory: project,
        agent_id: agent,
        model: {
          providerID: model.slice(0, slash),
          id: model.slice(slash + 1),
        },
        title: title || undefined,
        goal,
        mode: els.newMode.value,
        completion: els.newCompletion.value,
        max_tokens: maxTokens,
        max_turns: maxTurns,
        max_duration_s: Math.round(maxHours * 3600),
        start: els.newStart.checked,
      });
      els.newGoal.value = "";
      els.newTitle.value = "";
      toggleNewForm(false);
      await loadStatus();
      if (binding && binding.id) setSelected(binding.id);
      setStatus("Agent created.");
    } catch (error) {
      setNewStatus(error.message, true);
    } finally {
      creating = false;
      els.newSubmit.disabled = false;
    }
  }

  async function sendPrompt(event) {
    event.preventDefault();
    if (sending || !selectedId) return;
    const text = els.prompt.value.trim();
    if (!text) return;
    sending = true;
    els.send.disabled = true;
    setComposeStatus("Sending…");
    try {
      await post("/agents/" + encodeURIComponent(selectedId) + "/prompt", {
        text,
        request_id: randomId(),
        delivery: "queue",
      });
      els.prompt.value = "";
      setComposeStatus(
        "Sent. Chat pauses the autonomous loop; use /goal resume to continue it.",
      );
      await loadDetail();
      await loadStatus();
    } catch (error) {
      setComposeStatus(error.message, true);
    } finally {
      sending = false;
      els.send.disabled =
        !selected ||
        ["complete", "budget_exhausted", "limit_reached", "error"].includes(
          selected.status,
        );
    }
  }

  els.refresh.addEventListener("click", async () => {
    setStatus("Refreshing…");
    catalogCache.clear();
    catalog = null;
    catalogProject = "";
    commandSignature = "";
    await loadStatus();
    if (selectedId) await loadDetail();
  });
  els.reconnect.addEventListener("click", async () => {
    setStatus("Reconnecting to OpenCode…");
    try {
      await post("/agents/reconnect", {});
      catalogCache.clear();
      await loadStatus();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
  els.newButton.addEventListener("click", () => toggleNewForm());
  els.newCancel.addEventListener("click", () => toggleNewForm(false));
  els.newProject.addEventListener("change", loadNewCatalog);
  els.newMode.addEventListener("change", () => {
    els.newCompletion.value = els.newMode.value === "goal" ? "marker" : "manual";
  });
  els.newForm.addEventListener("submit", createAgent);
  els.compose.addEventListener("submit", sendPrompt);
  els.command.addEventListener("change", () => {
    const name = els.command.value;
    if (!name) return;
    els.prompt.value = "/" + name + " " + els.prompt.value;
    els.command.value = "";
    els.prompt.focus();
  });
  els.goal.addEventListener("input", () => {
    goalDirty = true;
  });
  els.goalSave.addEventListener("click", async () => {
    if (!selectedId) return;
    const objective = els.goal.value.trim();
    if (!objective) {
      setStatus("Objective cannot be empty.", true);
      return;
    }
    try {
      await post("/agents/" + encodeURIComponent(selectedId) + "/goal", {
        action: "set",
        objective,
        mode: els.mode.value,
        completion: els.completion.value,
      });
      goalDirty = false;
      setStatus("Objective saved and the loop resumed.");
      await loadDetail();
      await loadStatus();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
  els.pause.addEventListener("click", () =>
    control("goal", { action: "pause" }),
  );
  els.resume.addEventListener("click", () =>
    control("goal", { action: "resume" }),
  );
  els.stop.addEventListener("click", () =>
    control("interrupt", null, "Stop the current turn and pause this agent?"),
  );
  els.complete.addEventListener("click", () =>
    control(
      "goal",
      { action: "complete" },
      "Mark this agent complete and stop its loop?",
    ),
  );
  els.unbind.addEventListener("click", () =>
    control(
      "unbind",
      null,
      "Unbind this agent? The OpenCode session is retained; only the office binding is removed.",
    ),
  );
  els.transcript.addEventListener("scroll", () => {
    transcriptAtBottom =
      els.transcript.scrollTop + els.transcript.clientHeight >=
      els.transcript.scrollHeight - 24;
  });

  window.officeAgents = { open, close, refresh: loadStatus };
})();
