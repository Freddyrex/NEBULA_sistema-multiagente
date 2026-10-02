/* NEBULA · Chat flotante del agente (§8 gobernanza + SMAC §2.1/§2.5/§2.6/§5.2).
   Habla con el backend SMAC existente: POST /platform/smac/chat/
   {message, scenario} -> {reply, actions[]} y confirmación en
   POST /platform/smac/chat/confirm {id, confirm, confirm_name?}.
   Expone window.NebulaAgent (puente UI↔agente) y window.NebulaChat. */
(function () {
  "use strict";

  var fab = document.getElementById("nb-chat-fab");
  var chat = document.getElementById("nb-chat");
  if (!fab || !chat) return;
  var log = chat.querySelector(".nb-chat__log");
  var input = chat.querySelector("textarea");
  var sendBtn = chat.querySelector(".nb-chat__send");
  var statusDot = chat.querySelector(".nb-chat__status");
  var chipsBox = chat.querySelector(".nb-chat__chips");
  var ctxBox = document.getElementById("nb-chat-ctx");
  var ctxStatus = ctxBox ? ctxBox.querySelector(".nb-ctx__status") : null;
  var ctxName = ctxBox ? ctxBox.querySelector(".nb-ctx__name") : null;
  var ctxMeta = ctxBox ? ctxBox.querySelector(".nb-ctx__meta") : null;
  var dockBtn = chat.querySelector(".nb-chat__close")
    ? chat.querySelector(".nb-chat__dock") : null;
  var head = chat.querySelector(".nb-chat__head");
  var greeted = false;
  var greetedGeneric = false;
  var busy = false;
  var homeParent = chat.parentNode;
  var homeNext = chat.nextSibling;

  /* ---------- idioma (sigue a la UI; por defecto el del navegador) ---------- */
  function locale() {
    var ctx = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
    if (ctx.locale) return ctx.locale;
    try {
      var saved = localStorage.getItem("nb_locale");
      if (saved) return saved;
    } catch (e) {}
    return (navigator.language || "en").toLowerCase().indexOf("es") === 0 ? "es" : "en";
  }

  var T = {
    es: {
      open: "Abrir chat con el agente", close: "Cerrar chat con el agente",
      dock: "Acoplar chat al panel", undock: "Soltar chat como flotante",
      emptyLogin: "Hola. Inicia sesión para que pueda ayudarte con tus escenarios.",
      emptyNone: "Hola. Pregúntame por tus escenarios, nodos o resultados.",
      greetDone: function (c) { return "Este escenario terminó en " + c.rounds + " rondas. ¿Quieres un resumen de resultados?"; },
      greetRun: function (c) { return "El escenario va por la ronda " + c.round_visible + "/" + c.rounds + ". Puedo vigilar nodos lentos o anomalías."; },
      greetNosc: "Elige un escenario para empezar.",
      chipsDone: ["Resumir resultados", "¿Qué nodo rindió peor?"],
      chipsRun: ["¿Hay nodos lentos?", "Estado de la ronda"],
      chipNode: function (n) { return "Explicar el nodo " + n; },
      needLogin: "Inicia sesión primero: usa el menú Login de la barra superior.",
      needScenario: "Para actuar necesito un escenario. Abre uno desde el Dashboard y hablamos allí: veo el escenario abierto automáticamente.",
      degraded: "Nota: estoy en modo degradado (sin modelo en línea).",
      sessionExpired: "Tu sesión expiró. Vuelve a iniciar sesión.",
      connFail: "No se pudo conectar con el agente. ",
      confirmFail: "No se pudo confirmar la acción. Inténtalo de nuevo.",
      done: "Hecho: ", failed: "No se pudo ejecutar: ",
      confirm: "Confirmar", reject: "Rechazar",
      stopName: "Escribe el nombre del escenario para confirmar",
      placeholder: "Escribe un mensaje…",
      statusRun: "en ejecución", statusDone: "✔ completado",
      roundW: "ronda", nodesW: "nodos", activeW: "activos",
      pausedW: "en pausa", finishedW: "finalizados", offW: "offline",
      stragW: "rezagados", malW: "maliciosos", selNodeW: "nodo seleccionado",
      historicW: "histórico", runtimeEmptyW: "sin runtime"
    },
    en: {
      open: "Open agent chat", close: "Close agent chat",
      dock: "Dock chat to panel", undock: "Undock chat as floating",
      emptyLogin: "Hi. Log in so I can help with your scenarios.",
      emptyNone: "Hi. Ask me about your scenarios, nodes or results.",
      greetDone: function (c) { return "This scenario finished in " + c.rounds + " rounds. Want a results summary?"; },
      greetRun: function (c) { return "The scenario is at round " + c.round_visible + "/" + c.rounds + ". I can watch for slow nodes or anomalies."; },
      greetNosc: "Pick a scenario to get started.",
      chipsDone: ["Summarize results", "Which node performed worst?"],
      chipsRun: ["Any slow nodes?", "Round status"],
      chipNode: function (n) { return "Explain node " + n; },
      needLogin: "Log in first: use the Login menu in the top bar.",
      needScenario: "I need a scenario to act on. Open one from the Dashboard and we'll talk there: I see the open scenario automatically.",
      degraded: "Note: I'm in degraded mode (no live model).",
      sessionExpired: "Your session expired. Log in again.",
      connFail: "Could not reach the agent. ",
      confirmFail: "Could not confirm the action. Try again.",
      done: "Done: ", failed: "Could not run it: ",
      confirm: "Confirm", reject: "Dismiss",
      stopName: "Type the scenario name to confirm",
      placeholder: "Type a message…",
      statusRun: "running", statusDone: "✔ completed",
      roundW: "round", nodesW: "nodes", activeW: "running",
      pausedW: "paused", finishedW: "finished", offW: "offline",
      stragW: "stragglers", malW: "malicious", selNodeW: "selected node",
      historicW: "historic", runtimeEmptyW: "no runtime"
    }
  };
  function t(k) { return T[locale()][k]; }

  function sessionId() {
    var id = null;
    try {
      id = localStorage.getItem("nb_session");
      if (!id) {
        id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
        localStorage.setItem("nb_session", id);
      }
    } catch (e) { id = String(Date.now()); }
    return id;
  }

  /* Escenario actual: contexto del panel, input de la página, o URL */
  function currentScenario() {
    var ctx = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
    if (ctx.scenario_id) return ctx.scenario_id;
    var el = document.getElementById("smac-scenario");
    if (el && el.value.trim()) return el.value.trim();
    var m = location.pathname.match(/\/platform\/dashboard\/([^/]+)/);
    if (m) return decodeURIComponent(m[1]);
    return "";
  }

  function loggedIn() {
    return !!document.querySelector('input[id^="session_"][id$="_user"], #session_user');
  }

  function setStatus(state) { if (statusDot) statusDot.dataset.state = state; }

  /* ---------- barra de estado del escenario (§2.6) ----------
     El panel SMAC publica su snapshot en NebulaAgent.ctx tras cada fetch;
     aquí se refleja sin pedir nada al backend, para que el chat diga siempre
     en qué estado está la simulación (status, ronda, agregador, nodos). */
  function renderCtx() {
    if (!ctxBox) return;
    var ctx = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
    var sc = ctx.scenario_id || currentScenario();
    if (!sc || !ctx.scenario_status) { ctxBox.hidden = true; return; }
    var L = T[locale()];
    var st = String(ctx.scenario_status).toLowerCase();
    var isRunning = st === "running";
    var isDone = st === "completed" || st === "finished";

    ctxBox.hidden = false;
    ctxBox.dataset.state = isDone ? "done" : (isRunning ? "running" : "unknown");
    if (ctxStatus) {
      ctxStatus.dataset.state = ctxBox.dataset.state;
      ctxStatus.textContent = isDone ? L.statusDone : (isRunning ? L.statusRun : (ctx.scenario_status || "unknown"));
    }
    if (ctxName) { ctxName.textContent = sc; ctxName.title = sc; }

    var parts = [];
    var cur = (ctx.round_visible === null || ctx.round_visible === undefined) ? "—" : ctx.round_visible;
    parts.push(L.roundW + " " + cur + (ctx.rounds ? "/" + ctx.rounds : ""));
    if (ctx.aggregation) parts.push(ctx.aggregation);
    if (ctx.nodes_total !== undefined && ctx.nodes_total !== null) {
      parts.push(ctx.nodes_total + " " + L.nodesW);
      if (ctx.nodes_running) parts.push(ctx.nodes_running + " " + L.activeW);
      if (ctx.nodes_paused) parts.push(ctx.nodes_paused + " " + L.pausedW);
      if (ctx.nodes_finished) parts.push(ctx.nodes_finished + " " + L.finishedW);
      if (ctx.nodes_offline) parts.push(ctx.nodes_offline + " " + L.offW);
    }
    if (ctx.stragglers && ctx.stragglers.length) parts.push("🟠 " + L.stragW + ": " + ctx.stragglers.join(", "));
    if (ctx.malicious && ctx.malicious.length) parts.push("⚠ " + L.malW + ": " + ctx.malicious.join(", "));
    if (ctx.historic) parts.push(L.historicW);
    else if (ctx.runtime_empty) parts.push(L.runtimeEmptyW);
    if (ctx.selected_node !== undefined && ctx.selected_node !== null && ctx.selected_node !== "") {
      parts.push(L.selNodeW + " " + ctx.selected_node);
    }
    if (ctxMeta) ctxMeta.textContent = parts.join(" · ");
  }

  /* ---------- saludo y chips contextuales (§2.6) ---------- */
  function greet() {
    var ctx = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
    var sc = currentScenario();
    if (!loggedIn()) return t("emptyLogin");
    if (!sc) return (ctx.page && ctx.page.indexOf("/dashboard") === 0) ? t("greetNosc") : t("emptyNone");
    if (ctx.scenario_status === "completed") {
      return t("greetDone")({ rounds: ctx.rounds || "?" });
    }
    if (ctx.scenario_status === "running") {
      return t("greetRun")({ round_visible: ctx.round_visible || "?", rounds: ctx.rounds || "?" });
    }
    return t("emptyNone");
  }

  function renderChips() {
    if (!chipsBox) return;
    var ctx = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
    var L = T[locale()];
    var list = [];
    if (ctx.scenario_status === "completed") list = L.chipsDone.slice(0, 3);
    else if (ctx.scenario_status === "running") list = L.chipsRun.slice(0, 3);
    if (ctx.selected_node !== undefined && ctx.selected_node !== null && ctx.selected_node !== "") {
      list = list.slice(0, 2).concat([L.chipNode(ctx.selected_node)]);
    }
    chipsBox.innerHTML = "";
    if (!list.length) { chipsBox.style.display = "none"; return; }
    chipsBox.style.display = "";
    list.forEach(function (text) {
      var b = document.createElement("button");
      b.className = "nb-chip";
      b.type = "button";
      b.textContent = text;
      b.dataset.prompt = text;
      chipsBox.appendChild(b);
    });
  }

  function toggleChat(open) {
    if (open === undefined) open = chat.dataset.open !== "true";
    chat.hidden = false;
    requestAnimationFrame(function () { chat.dataset.open = String(open); });
    fab.dataset.open = String(open);
    fab.setAttribute("aria-expanded", String(open));
    fab.setAttribute("aria-label", open ? t("close") : t("open"));
    if (open) {
      fab.dataset.notify = "false";
      if (!greeted) {
        greeted = true;
        var ctxNow = (window.NebulaAgent && window.NebulaAgent.ctx) || {};
        greetedGeneric = !ctxNow.scenario_status;
        addMsg("agent", greet());
      }
      renderChips();
      renderCtx();
      input.placeholder = t("placeholder");
      input.focus();
    } else {
      if (document.body.contains(fab) && getComputedStyle(fab).display !== "none") fab.focus();
      setTimeout(function () { if (chat.dataset.open !== "true") chat.hidden = true; }, 240);
    }
  }

  fab.addEventListener("click", function () { toggleChat(); });
  chat.querySelector(".nb-chat__close").addEventListener("click", function () { toggleChat(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && chat.dataset.open === "true") toggleChat(false);
  });

  /* ---------- acoplado / flotante + arrastre (§2.1) ---------- */
  var slot = document.getElementById("smac-chat-slot");
  var layout = document.getElementById("smac-layout");
  function docked() {
    /* Por defecto acoplado en el panel SMAC (D1: no tapa controles);
       el usuario puede soltarlo y se recuerda la preferencia. */
    try {
      var saved = localStorage.getItem("nb_chat_mode");
      if (saved) return saved === "docked" && !!slot;
      return !!slot && window.innerWidth >= 1280;
    } catch (e) { return false; }
  }
  function applyDock(mode) {
    if (!slot || !layout) return;
    if (mode === "docked") {
      slot.hidden = false;
      slot.appendChild(chat);
      layout.dataset.chat = "docked";
      document.body.classList.add("nb-chat-docked");
      chat.style.left = ""; chat.style.top = ""; chat.style.right = ""; chat.style.bottom = "";
      try { localStorage.setItem("nb_chat_mode", "docked"); } catch (e) {}
      if (dockBtn) { dockBtn.setAttribute("aria-label", t("undock")); dockBtn.title = t("undock"); }
      if (chat.dataset.open === "true") { chat.hidden = false; }
      toggleChat(true); /* acoplado = columna siempre visible */
    } else {
      homeParent.insertBefore(chat, homeNext);
      slot.hidden = true;
      layout.dataset.chat = "float";
      document.body.classList.remove("nb-chat-docked");
      try { localStorage.setItem("nb_chat_mode", "float"); } catch (e) {}
      if (dockBtn) { dockBtn.setAttribute("aria-label", t("dock")); dockBtn.title = t("dock"); }
      restoreFloatPos();
    }
  }
  if (dockBtn) {
    if (slot && layout) {
      dockBtn.hidden = false;
      dockBtn.addEventListener("click", function () {
        applyDock(docked() ? "float" : "docked");
      });
      if (docked()) applyDock("docked");
    } else {
      dockBtn.hidden = true;
    }
  }
  /* Arrastre en modo flotante (se recuerda la posición) */
  function restoreFloatPos() {
    try {
      var p = JSON.parse(localStorage.getItem("nb_chat_pos") || "null");
      if (p && typeof p.x === "number") {
        chat.style.left = p.x + "px"; chat.style.top = p.y + "px";
        chat.style.right = "auto"; chat.style.bottom = "auto";
      }
    } catch (e) {}
  }
  (function drag() {
    if (!head) return;
    var sx, sy, ox, oy, on = false;
    head.style.cursor = "move";
    head.addEventListener("pointerdown", function (e) {
      if (e.target.closest("button")) return;
      if (docked() || window.innerWidth < 1280 && layout) { /* en dock/móvil no se arrastra */ }
      if (layout && layout.dataset.chat === "docked" && window.innerWidth >= 1280) return;
      var r = chat.getBoundingClientRect();
      chat.style.left = r.left + "px"; chat.style.top = r.top + "px";
      chat.style.right = "auto"; chat.style.bottom = "auto";
      sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top; on = true;
      head.setPointerCapture(e.pointerId);
    });
    head.addEventListener("pointermove", function (e) {
      if (!on) return;
      chat.style.left = Math.max(0, ox + e.clientX - sx) + "px";
      chat.style.top = Math.max(0, oy + e.clientY - sy) + "px";
    });
    head.addEventListener("pointerup", function () {
      if (!on) return;
      on = false;
      try {
        localStorage.setItem("nb_chat_pos", JSON.stringify({
          x: parseInt(chat.style.left, 10), y: parseInt(chat.style.top, 10)
        }));
      } catch (e) {}
    });
  })();
  restoreFloatPos();

  function addMsg(role, text) {
    var el = document.createElement("div");
    el.className = "nb-msg nb-msg--" + role;
    el.textContent = text || "";
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function addTyping() {
    var el = document.createElement("div");
    el.className = "nb-msg nb-msg--agent nb-msg--typing";
    el.setAttribute("aria-label", "…");
    el.innerHTML = "<span>●</span><span>●</span><span>●</span>";
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function hideChips() { if (chipsBox) chipsBox.style.display = "none"; }

  if (chipsBox) chipsBox.addEventListener("click", function (e) {
    var chip = e.target.closest(".nb-chip");
    if (!chip) return;
    input.value = chip.dataset.prompt || chip.textContent;
    input.focus();
  });

  /* Tarjeta de acción con confirmación explícita */
  function addActionCard(action, scenario) {
    var card = document.createElement("div");
    card.className = "nb-msg nb-msg--action";
    var title = document.createElement("div");
    title.textContent = (action.description || action.kind || "?");
    card.appendChild(title);

    var btns = document.createElement("div");
    btns.className = "nb-action__btns";
    var okBtn = document.createElement("button");
    okBtn.className = "nb-action__confirm";
    okBtn.textContent = t("confirm");
    var noBtn = document.createElement("button");
    noBtn.className = "nb-action__reject";
    noBtn.textContent = t("reject");
    btns.appendChild(okBtn);
    btns.appendChild(noBtn);
    card.appendChild(btns);

    var nameInput = null;
    if (action.kind === "stop_scenario") {
      nameInput = document.createElement("input");
      nameInput.className = "nb-action__name";
      nameInput.placeholder = t("stopName");
      nameInput.setAttribute("aria-label", t("stopName"));
      card.appendChild(nameInput);
      var toggleName = function () { okBtn.disabled = (nameInput.value.trim() !== scenario); };
      nameInput.addEventListener("input", toggleName);
      toggleName();
    }

    okBtn.addEventListener("click", function () {
      okBtn.disabled = true; noBtn.disabled = true;
      confirmAction(action.id, true, nameInput ? nameInput.value.trim() : "", card);
    });
    noBtn.addEventListener("click", function () {
      okBtn.disabled = true; noBtn.disabled = true;
      confirmAction(action.id, false, "", card);
    });
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  }

  function confirmAction(id, confirm, confirmName, card) {
    fetch("/platform/smac/chat/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ id: id, confirm: confirm, confirm_name: confirmName || undefined })
    })
      .then(function (res) { return res.json().then(function (d) { return { ok: res.ok, d: d }; }); })
      .then(function (r) {
        var note = document.createElement("div");
        note.style.marginTop = "8px";
        note.style.fontSize = "13px";
        note.textContent = r.ok
          ? (t("done") + (r.d.result || r.d.message || "…"))
          : (t("failed") + (r.d.detail || r.d.error || "…"));
        card.appendChild(note);
        log.scrollTop = log.scrollHeight;
      })
      .catch(function () { addMsg("agent", t("confirmFail")); });
  }

  function send(message, ctxOverride) {
    if (busy || !message) return;
    var ctx = ctxOverride || ((window.NebulaAgent && window.NebulaAgent.ctx) || {});
    if (!loggedIn()) {
      addMsg("user", message);
      addMsg("agent", t("needLogin"));
      return;
    }
    var scenario = ctx.scenario_id || currentScenario();
    addMsg("user", message);
    hideChips();
    if (!scenario) {
      addMsg("agent", t("needScenario"));
      return;
    }
    busy = true;
    setStatus("busy");
    var typing = addTyping();
    fetch("/platform/smac/chat/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ message: message, scenario: scenario, request_id: sessionId() })
    })
      .then(function (res) {
        if (res.status === 401) throw new Error("auth");
        if (res.status === 429) throw new Error("429");
        return res.json().then(function (d) {
          if (!res.ok) throw new Error(d.detail || d.error || "…");
          return d;
        });
      })
      .then(function (data) {
        typing.remove();
        addMsg("agent", data.reply || "(…)");
        (data.actions || []).forEach(function (a) { addActionCard(a, scenario); });
        if (data.degraded) addMsg("agent", t("degraded"));
      })
      .catch(function (err) {
        typing.remove();
        setStatus("offline");
        addMsg("agent", err.message === "auth" ? t("sessionExpired") : t("connFail") + err.message);
        setTimeout(function () { setStatus("ready"); }, 5000);
      })
      .finally(function () { busy = false; setStatus("ready"); });
  }

  function submit() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    send(text);
  }

  sendBtn.addEventListener("click", submit);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && input.value.trim()) {
      e.preventDefault();
      submit();
    }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  /* ---------- puente UI ↔ agente (§5.2) ---------- */
  window.NebulaAgent = window.NebulaAgent || {};
  window.NebulaAgent.ctx = window.NebulaAgent.ctx || {};
  window.NebulaAgent.setContext = function (patch) {
    var prevStatus = (window.NebulaAgent.ctx || {}).scenario_status;
    window.NebulaAgent.ctx = Object.assign({}, window.NebulaAgent.ctx, patch || {});
    /* La barra de estado se repinta con cualquier parche: el panel publica
       su snapshot cada ~5 s, así el chat refleja el estado en vivo (§2.6). */
    if (patch) renderCtx();
    if (patch && patch.locale) {
      try { localStorage.setItem("nb_locale", patch.locale); } catch (e) {}
      /* Cambio de idioma con el chat abierto: se re-traduce el saludo
         (solo si aún no hay conversación) + chips + placeholder (§2.6). */
      var openMsgs = log.querySelectorAll(".nb-msg");
      if (openMsgs.length <= 1) {
        greeted = false;
        log.innerHTML = "";
        if (chat.dataset.open === "true") {
          greeted = true;
          greetedGeneric = !((window.NebulaAgent.ctx || {}).scenario_status);
          addMsg("agent", greet());
        }
      }
      renderChips();
      input.placeholder = t("placeholder");
    }
    /* El panel publica su contexto tras el primer fetch: si el saludo fue
       genérico, se sustituye por el contextual (§2.6). */
    if (patch && patch.scenario_status && greeted && greetedGeneric) {
      var msgs = log.querySelectorAll(".nb-msg");
      if (msgs.length === 1) {
        msgs[0].textContent = greet();
        greetedGeneric = false;
        renderChips();
      }
    }
    /* Cambio de estado real (running ↔ completed) con el saludo aún en
       pantalla: se actualiza para que no contradiga a la barra de estado. */
    if (patch && patch.scenario_status && prevStatus && patch.scenario_status !== prevStatus) {
      var shown = log.querySelectorAll(".nb-msg");
      if (shown.length === 1) shown[0].textContent = greet();
      if (chipsBox && chipsBox.style.display !== "none") renderChips();
    }
  };
  window.NebulaAgent.ask = function (text, opts) {
    opts = opts || {};
    if (opts.open) toggleChat(true);
    else if (chat.dataset.open !== "true") { toggleChat(true); }
    send(text, opts.ctx);
  };
  window.NebulaAgent.notify = function () {
    if (chat.dataset.open !== "true") fab.dataset.notify = "true";
  };
  window.NebulaChat = { toggle: toggleChat, send: send };
})();
