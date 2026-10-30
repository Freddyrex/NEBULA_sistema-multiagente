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

  /* ---------- historial por escenario (una sección por escenario) ----------
     Cada escenario guarda su propia conversación (localStorage) y empieza
     de 0 la primera vez. Sin escenario se usa la sección "__general__". */
  var CFG = window.NebulaConfig || {};
  var API = window.NebulaApi || {};
  var BREAKPOINT_DOCK = CFG.breakpointDock || 1280;
  var CHIPS_MAX = CFG.chipsMax || 3;
  var READY_RESET_MS = CFG.chatReadyResetMs || 5000;
  var _mqDock = (window.matchMedia ? window.matchMedia("(min-width: " + BREAKPOINT_DOCK + "px)") : null);
  var HIST_KEY = "nb_chat_hist_v1";
  var HIST_MAX_MSG = CFG.histMaxMsg || 60,
    HIST_MAX_SC = CFG.histMaxSc || 15,
    HIST_MAX_LEN = CFG.histMaxLen || 2000;
  var GLOBAL_SECTION = "__general__";
  var activeScenario = "";
  function histKey(sc) { return (sc || "").trim() || GLOBAL_SECTION; }
  function histLoad() {
    try {
      var d = JSON.parse(localStorage.getItem(HIST_KEY) || "{}");
      return (d && typeof d === "object") ? d : {};
    } catch (e) { return {}; }
  }
  function histSaveAll(d) {
    try {
      var keys = Object.keys(d).filter(function (k) { return Array.isArray(d[k]) && d[k].length; });
      keys.sort(function (a, b) {
        var ta = ((d[a][d[a].length - 1]) || {}).ts || 0;
        var tb = ((d[b][d[b].length - 1]) || {}).ts || 0;
        return ta - tb;
      });
      var out = {};
      keys.slice(-HIST_MAX_SC).forEach(function (k) { out[k] = d[k].slice(-HIST_MAX_MSG); });
      localStorage.setItem(HIST_KEY, JSON.stringify(out));
    } catch (e) {}
  }
  function histGet(sc) {
    var h = histLoad()[histKey(sc)];
    return Array.isArray(h) ? h : [];
  }
  function histPush(sc, role, text) {
    if (!text) return;
    var d = histLoad();
    var k = histKey(sc);
    var h = Array.isArray(d[k]) ? d[k] : [];
    h.push({ role: role, text: String(text).slice(0, HIST_MAX_LEN), ts: Date.now() });
    d[k] = h.slice(-HIST_MAX_MSG);
    histSaveAll(d);
  }
  function histClear(sc) {
    var d = histLoad();
    delete d[histKey(sc)];
    histSaveAll(d);
  }
  function sectionDivider(sc) {
    var el = document.createElement("div");
    el.className = "nb-sec";
    el.textContent = "— " + t("secScenario") + " " + (sc.trim() || "…") + " —";
    return el;
  }
  /* Divisor de continuidad: el hilo sigue del escenario anterior. */
  function carryDivider(fromSc) {
    var el = document.createElement("div");
    el.className = "nb-sec nb-sec--carry";
    el.textContent = t("carryOn")(fromSc || t("carryGeneral"));
    return el;
  }
  /* Sección con mensajes más reciente (la "simulación anterior"), excluyendo
     la actual: es la fuente del arrastre cuando el operador pasa a un
     escenario nuevo. */
  function lastSectionWithMessages(exceptKey) {
    var d = histLoad();
    var best = null, bestTs = -1;
    Object.keys(d).forEach(function (k) {
      if (k === exceptKey || k === GLOBAL_SECTION) return;
      var arr = d[k];
      if (!Array.isArray(arr) || !arr.length) return;
      var ts = (arr[arr.length - 1] || {}).ts || 0;
      if (ts >= bestTs) { bestTs = ts; best = k; }
    });
    return best;
  }
  /* Continúa la conversación: si el escenario nuevo no tiene historial, se
     arrastra el hilo del escenario anterior (visible + persistido), de modo
     que el chat no empieza de cero al mover el operador a la simulación
     siguiente. */
  function carryOver(srcKey, toSc) {
    if (!srcKey) return false;
    var d = histLoad();
    var src = d[srcKey];
    if (!Array.isArray(src) || !src.length) return false;
    var toKey = histKey(toSc);
    if (Array.isArray(d[toKey]) && d[toKey].length) return false;
    d[toKey] = src.map(function (m) {
      return { role: m.role, text: m.text, ts: m.ts, carriedFrom: srcKey };
    });
    histSaveAll(d);
    return true;
  }
  function switchScenario(sc) {
    sc = (sc || "").trim();
    if (sc === activeScenario) return;
    var previous = activeScenario;
    activeScenario = sc;
    log.textContent = "";
    var h = histGet(sc);
    if (!h.length && sc) {
      /* Escenario nuevo: se continúa el hilo del anterior (si lo hay).
         Solo para escenarios con nombre: la sección general (dashboard) no
         se rellena con la copia de otra simulación. */
      var srcKey = null;
      if (previous && previous !== sc && histGet(previous).length) srcKey = histKey(previous);
      if (!srcKey) srcKey = lastSectionWithMessages(histKey(sc));
      if (srcKey && srcKey !== histKey(sc) && carryOver(srcKey, sc)) {
        h = histGet(sc);
        log.appendChild(carryDivider(srcKey === GLOBAL_SECTION ? "" : srcKey));
        h.forEach(function (m) { addMsg(m.role, m.text, { persist: false }); });
        greeted = true;
        greetedGeneric = false;
        renderChips();
        renderCtx();
        return;
      }
    }
    if (h.length) {
      log.appendChild(sectionDivider(sc));
      h.forEach(function (m) { addMsg(m.role, m.text, { persist: false }); });
      greeted = true;
      greetedGeneric = false;
      renderChips();
      renderCtx();
    } else {
      /* Sección nueva: se empieza de 0; el saludo fluye al abrir. */
      greeted = false;
      greetedGeneric = false;
      if (chat.dataset.open === "true") {
        greeted = true;
        addMsg("agent", greet());
        renderChips();
        renderCtx();
      }
    }
  }
  function clearActiveSection() {
    histClear(activeScenario);
    log.textContent = "";
    greeted = false;
    greetedGeneric = false;
    if (chat.dataset.open === "true") {
      greeted = true;
      addMsg("agent", greet());
      renderChips();
      renderCtx();
    }
  }

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
      newChat: "Empezar de 0 (borra el historial de este escenario)",
      secScenario: "Escenario",
      carryOn: function (sc) { return "↩ Continúa la conversación de " + sc; },
      carryGeneral: "la conversación general",
      degraded: "Nota: estoy en modo degradado (sin modelo en línea).",
      sessionExpired: "Tu sesión expiró. Vuelve a iniciar sesión.",
      connFail: "No se pudo conectar con el agente. ",
      rateLimited: "Demasiados mensajes seguidos. Espera unos segundos e inténtalo de nuevo.",
      confirmFail: "No se pudo confirmar la acción. Inténtalo de nuevo.",
      done: "Hecho: ", failed: "No se pudo ejecutar: ",
      confirm: "Confirmar", reject: "Rechazar", goTo: "Abrir",
      propose: "Proponer", autoPaused: "Simulación pausada por sugerencia pendiente.",
      resumeBtn: "Reanudar", resumed: "Simulación reanudada.",
      accept: "Aceptar", decline: "Rechazar",
      suggestionWhy: "por qué", before: "antes", after: "después",
      risk: "riesgo", keepAsIs: "Continuar sin cambiar",
      partialWarn: "Cambio PARCIAL: algunos nodos no confirmaron. La simulación sigue pausada.",
      resumedOk: "Cambio aplicado. Simulación reanudada.",
      resumeAsk: "¿Reanudar ahora?",
      perNode: "resultado por nodo", nodesMissing: "sin respuesta",
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
      newChat: "Start over (clear this scenario's history)",
      secScenario: "Scenario",
      carryOn: function (sc) { return "↩ Continuing the conversation from " + sc; },
      carryGeneral: "the general chat",
      degraded: "Note: I'm in degraded mode (no live model).",
      sessionExpired: "Your session expired. Log in again.",
      connFail: "Could not reach the agent. ",
      rateLimited: "Too many messages in a row. Wait a few seconds and try again.",
      confirmFail: "Could not confirm the action. Try again.",
      done: "Done: ", failed: "Could not run it: ",
      confirm: "Confirm", reject: "Dismiss", goTo: "Open",
      propose: "Propose", autoPaused: "Simulation paused for pending suggestion.",
      resumeBtn: "Resume", resumed: "Simulation resumed.",
      accept: "Accept", decline: "Dismiss",
      suggestionWhy: "why", before: "before", after: "after",
      risk: "risk", keepAsIs: "Continue without changing",
      partialWarn: "PARTIAL change: some nodes did not confirm. The run stays paused.",
      resumedOk: "Change applied. Simulation resumed.",
      resumeAsk: "Resume now?",
      perNode: "per-node result", nodesMissing: "no response",
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
    if (ctx.scenario_status === "completed") list = L.chipsDone.slice(0, CHIPS_MAX);
    else if (ctx.scenario_status === "running") list = L.chipsRun.slice(0, CHIPS_MAX);
    if (ctx.selected_node !== undefined && ctx.selected_node !== null && ctx.selected_node !== "") {
      list = list.slice(0, Math.max(0, CHIPS_MAX - 1)).concat([L.chipNode(ctx.selected_node)]);
    }
    chipsBox.textContent = "";
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
      return !!slot && (_mqDock ? _mqDock.matches : window.innerWidth >= BREAKPOINT_DOCK);
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
  try { setupPermSelector(); } catch (e) {}
  /* Arrastre en modo flotante (se recuerda la posición) */
  function restoreFloatPos() {
    var head = document.querySelector("#nb-chat .nb-chat__head");
    if (!head || head.querySelector(".nb-perm")) return;
    var sel = document.createElement("select");
    sel.className = "nb-perm";
    sel.title = t("permissionMode");
    var o1 = document.createElement("option"); o1.value = "ask"; o1.textContent = t("askAlways"); sel.appendChild(o1);
    var o2 = document.createElement("option"); o2.value = "allowAgg"; o2.textContent = t("allowAggregator"); sel.appendChild(o2);
    sel.addEventListener("change", function () { setPermMode(currentScenario(), sel.value); });
    sel.value = getPermMode(currentScenario());
    var dock = head.querySelector(".nb-chat__dock");
    if (dock) head.insertBefore(sel, dock); else head.appendChild(sel);
  }
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
      if (docked() || (_mqDock ? !_mqDock.matches : window.innerWidth < BREAKPOINT_DOCK) && layout) { /* en dock/móvil no se arrastra */ }
      if (layout && layout.dataset.chat === "docked" && (_mqDock ? _mqDock.matches : window.innerWidth >= BREAKPOINT_DOCK)) return;
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

  function addMsg(role, text, opts) {
    var el = document.createElement("div");
    el.className = "nb-msg nb-msg--" + role;
    el.textContent = text || "";
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    if (!opts || opts.persist !== false) histPush(activeScenario, role, text);
    return el;
  }

  /* Racional por agente del crew (solo cuando el flag multi_agent está on). */
  function renderCrewRationale(crew) {
    if (!crew || !crew.trace) return;
    var verdicts = crew.trace.verdicts || [];
    if (!verdicts.length) return;
    var box = document.createElement("div");
    box.className = "nb-msg nb-msg--agent nb-msg--crew";
    verdicts.forEach(function (v) {
      var line = document.createElement("div");
      var agent = v.agent || v.agent_key || "agent";
      var reasons = (v.patterns && Object.keys(v.patterns)) || [];
      line.textContent = "· " + agent + ": " + (reasons.join(", ") || v.fallback || "sin cambio");
      box.appendChild(line);
    });
    log.appendChild(box);
    log.scrollTop = log.scrollHeight;
  }

  function addTyping() {
    var el = document.createElement("div");
    el.className = "nb-msg nb-msg--agent nb-msg--typing";
    el.setAttribute("aria-label", "…");
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement("span");
      dot.textContent = "●";
      el.appendChild(dot);
    }
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
    /* Navegación: sin confirmación, botón directo (ver addNavCard). */
    if (action && action.kind === "navigate") return;
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

  /* Destinos de navegación: un botón por destino, en una sola tarjeta. */
  function addNavCard(navs) {
    if (!navs || !navs.length) return;
    var btns = document.createElement("div");
    btns.className = "nb-action__btns";
    navs.forEach(function (a) {
      var url = a && a.params && a.params.url;
      if (!url) return;
      var btn = document.createElement("button");
      btn.className = "nb-action__confirm";
      btn.textContent = "→ " + (a.description || t("goTo"));
      btn.addEventListener("click", function () { window.location.href = url; });
      btns.appendChild(btn);
    });
    if (!btns.childNodes.length) return;
    var card = document.createElement("div");
    card.className = "nb-msg nb-msg--action";
    card.appendChild(btns);
    log.appendChild(card);
    log.scrollTop = log.scrollHeight;
  }

  function confirmAction(id, confirm, confirmName, card) {
    fetch(API.smacConfirm || "/platform/smac/chat/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ id: id, confirm: confirm, confirm_name: confirmName || undefined })
    })
      .then(readJson)
      .then(function (r) {
        var note = document.createElement("div");
        note.style.marginTop = "8px";
        note.style.fontSize = "13px";
        note.textContent = r.ok
          ? (t("done") + (r.d.detail || r.d.result || r.d.message || "…"))
          : (t("failed") + (r.d.detail || r.d.error || "…"));
        card.appendChild(note);
        /* Fase 3: resultado por nodo + parcial no reanuda en silencio. */
        if (confirm && r.d && (r.d.node_results !== undefined || r.d.partial !== undefined)) {
          try { _renderNodeResults(card, r, currentScenario()); } catch (e) {}
        }
        if (card && card.__setState) {
          try { card.__setState(r.ok ? "stateDone" : (r.d && r.d.error ? "stateFailed" : "stateRejected")); } catch (e) {}
        }
        log.scrollTop = log.scrollHeight;
      })
      .catch(function (err) {
        addMsg("agent", t("confirmFail") + (err && err.message ? " (" + err.message + ")" : ""));
      });
  }

  /* Lee la respuesta como JSON sin romper si el servidor o un proxy devuelve
     HTML/texto plano (404, 502, página de login...). Devuelve
     {ok, status, d} y lanza un Error legible cuando el cuerpo no es JSON. */
  function readJson(res) {
    return res.text().then(function (text) {
      var data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          var plain = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
          throw new Error("HTTP " + res.status + (plain ? ": " + plain.slice(0, 160) : ""));
        }
      }
      return { ok: res.ok, status: res.status, d: data || {} };
    });
  }

  /* Distingue un fallo de red real de un error HTTP ya interpretado. */
  function isNetworkError(err) {
    var m = String((err && err.message) || "");
    return /failed to fetch|networkerror|load failed|network request failed/i.test(m);
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
    if (!scenario && !wantsNewScenario(message)) {
      addMsg("agent", t("needScenario"));
      return;
    }
    busy = true;
    setStatus("busy");
    var typing = addTyping();
    fetch(API.smacChat || "/platform/smac/chat/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ message: message, scenario: scenario, request_id: sessionId() })
    })
      .then(function (res) {
        if (res.status === 401) throw new Error("auth");
        if (res.status === 429) throw new Error(t("rateLimited"));
        return readJson(res).then(function (r) {
          if (!r.ok) throw new Error(r.d.detail || r.d.error || ("HTTP " + r.status));
          return r.d;
        });
      })
      .then(function (data) {
        typing.remove();
        addMsg("agent", data.reply || "(…)");
        renderCrewRationale(data.crew);
        var acts = data.actions || [];
        var navs = acts.filter(function (a) { return a && a.kind === "navigate"; });
        acts.filter(function (a) { return !(a && a.kind === "navigate"); })
            .forEach(function (a) { addActionCard(a, scenario); });
        if (navs.length) addNavCard(navs);
        if (data.degraded) addMsg("agent", t("degraded"));
      })
      .catch(function (err) {
        typing.remove();
        setStatus("offline");
        if (err.message === "auth") addMsg("agent", t("sessionExpired"));
        else if (err.message === t("rateLimited")) addMsg("agent", err.message);
        else if (isNetworkError(err)) addMsg("agent", t("connFail") + err.message);
        else addMsg("agent", err.message);
        setTimeout(function () { setStatus("ready"); }, READY_RESET_MS);
      })
      .finally(function () { busy = false; setStatus("ready"); });
  }

  /* Espejo local de backend wants_new_scenario (el backend revalida):
     sin escenario abierto, solo la creación viaja al servidor. */
  function wantsNewScenario(text) {
    var t = (text || "").toLowerCase();
    var pats = [
      /nuev[oa]\s+escenario/, /crea(r)?\s+(un\s+|una\s+|el\s+|otro\s+)?escenario/,
      /creemos\s+(un\s+)?escenario/, /vamos\s+a\s+crear/,
      /montar\s+(un\s+)?escenario/, /lanzar\s+(un\s+)?escenario/,
      /empezar\s+un\s+escenario/, /new\s+scenario/,
      /creat(e|ing)\s+(a\s+)?(new\s+)?scenario/, /let'?s\s+create\s+(a\s+)?(new\s+)?scenario/,
      /start\s+a\s+new\s+scenario/
    ];
    for (var i = 0; i < pats.length; i++) { if (pats[i].test(t)) return true; }
    return false;
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

  /* Sugerencias del agente en el chat (sin salir al panel): una sola tarjeta
     con Aceptar/Rechazar. Aceptar encadena /propose → /chat/confirm (token
     single-use oculto). Rechazar descarta y reanuda. Muestra antes→después,
     evidencia y riesgo; resultado por nodo; parcial NO reanuda en silencio. */
  var seenSugg = {};
  function suggestionKey(sc, id) { return (sc || "?") + "::" + (id || "?"); }

  /* Fase 3: el viewer ve la tarjeta pero sin botones de acción. */
  function readOnly() {
    var r = window.NebulaRole;
    if (!r) return false;
    r = String(r).toLowerCase();
    return r !== "admin" && r !== "operator";
  }

  function _changeLabel(change) {
    if (!change || typeof change !== "object") return "";
    if (change.kind === "aggregator") return change.algorithm || "?";
    if (change.kind === "federation") return change.to_federation || "?";
    if (change.kind === "topology") return change.to_topology || "?";
    if (change.kind === "batch") return "batch=" + change.batch_size;
    if (change.kind === "rounds") return "rounds=" + change.total_rounds;
    if (change.kind === "model") return change.to_model || "?";
    return change.kind || "?";
  }

  function _addLine(host, label, value, cls) {
    if (value === undefined || value === null || value === "") return;
    var line = document.createElement("div");
    line.className = "nb-action__line" + (cls ? " " + cls : "");
    line.textContent = label ? label + ": " + value : String(value);
    host.appendChild(line);
  }

  function _renderNodeResults(host, r, sc) {
    var res = r.d || {};
    _addLine(host, t("perNode"), String((res.nodes_ok || []).length) + " ok",
             res.partial ? "text-warning" : "");
    if (res.nodes_missing && res.nodes_missing.length) {
      _addLine(host, t("nodesMissing"), res.nodes_missing.join(", "), "text-warning");
    }
    if (res.nodes_failed && res.nodes_failed.length) {
      _addLine(host, t("failed"), res.nodes_failed.join(", "), "text-danger");
    }
    if (res.partial) {
      _addLine(host, "", t("partialWarn"), "text-warning");
      var ask = document.createElement("div");
      ask.className = "nb-action__btns";
      var yes = document.createElement("button");
      yes.className = "nb-action__confirm";
      yes.textContent = t("resumeAsk");
      yes.addEventListener("click", function () {
        yes.disabled = true;
        fetch(API.smacResume || "/platform/smac/resume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin", body: JSON.stringify({ scenario: sc })
        })
          .then(function () { addMsg("agent", t("resumedOk")); })
          .catch(function () { addMsg("agent", t("confirmFail")); });
      });
      ask.appendChild(yes);
      host.appendChild(ask);
    } else {
      _addLine(host, "", t("resumedOk"), "text-success");
    }
  }

  function maybeShowSuggestions() {
    var ctx = window.NebulaAgent.ctx || {};
    var sc = ctx.scenario_id || currentScenario();
    if (!sc) return;
    var list = Array.isArray(ctx.suggestions) ? ctx.suggestions : [];
    var fresh = [];
    list.forEach(function (s) {
      if (!s) return;
      var key = suggestionKey(sc, s.id || (s.kind + JSON.stringify(s.params || {})));
      if (!seenSugg[key]) { seenSugg[key] = true; fresh.push(s); }
    });
    if (!fresh.length) return;
    if (window.NebulaAgent.notify) window.NebulaAgent.notify();
    fresh.forEach(function (s) {
      var change = (s.params && s.params.change) || {};
      var previous = (s.params && s.params.previous) || null;
      var from = previous !== null && previous !== undefined ? previous : "?";
      var to = _changeLabel(change);
      /* Fase 6: modo de permisos — permitir agregador en esta simulación (solo select_aggregator). */
      var sc2 = sc;
      var permMode = "ask";
      try { permMode = localStorage.getItem("smac_perm_mode:" + (sc2 || "default")) || "ask"; } catch (e) {}
      if (permMode === "allowAgg" && s.kind === "select_aggregator" && !readOnly()) {
        fetch(API.smacPropose || "/platform/smac/propose", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ scenario: sc2, kind: s.kind, params: s.params || {},
                                 description: (s.title || s.kind) + ": " + (s.reason || "") })
        })
          .then(readJson)
          .then(function (r) {
            if (r.ok && r.d && r.d.id) {
              fetch(API.smacConfirm || "/platform/smac/chat/confirm", {
                method: "POST", headers: { "Content-Type": "application/json" },
                credentials: "same-origin", body: JSON.stringify({ id: r.d.id, confirm: true })
              }).then(function () {}).catch(function () {});
            }
          })
          .catch(function () {});
        return;
      }
      addMsg("agent", "💡 " + (s.title || s.kind) + " — " + from + " → " + to);
      var card = document.createElement("div");
      card.className = "nb-msg nb-msg--action";
      /* por qué: evidencia */
      var ev = Array.isArray(s.evidence) ? s.evidence.slice(0, 4) : [];
      if (ev.length) _addLine(card, t("suggestionWhy"), ev.join("; "));
      else if (s.reason) _addLine(card, t("suggestionWhy"), s.reason);
      /* antes → después */
      _addLine(card, t("before") + " → " + t("after"), from + " → " + to);
      /* riesgo / severidad */
      _addLine(card, t("risk"), s.severity || "info");
      var btns = document.createElement("div");
      btns.className = "nb-action__btns";
      var ok = document.createElement("button");
      ok.className = "nb-action__confirm";
      ok.textContent = t("accept");
      var no = document.createElement("button");
      no.className = "nb-action__reject";
      no.textContent = t("decline");
      btns.appendChild(ok);
      btns.appendChild(no);
      card.appendChild(btns);
      var keep = document.createElement("div");
      keep.className = "nb-action__line small";
      keep.textContent = t("keepAsIs");
      card.appendChild(keep);
      /* Fase 6: máquina de estados visible en la tarjeta. */
      var stateLine = document.createElement("div");
      stateLine.className = "nb-action__line small text-muted";
      stateLine.textContent = t("stateProposed");
      card.appendChild(stateLine);
      function setState(key) { stateLine.textContent = t(key); }
      card.__setState = setState;
      if (readOnly()) {
        /* Viewer: tarjeta informativa, sin acciones. */
        ok.style.display = "none";
        no.style.display = "none";
        keep.textContent = t("needsConfirm");
        setState("stateWaiting");
        log.appendChild(card);
        log.scrollTop = log.scrollHeight;
        return;
      }

      function doConfirm(actionId, confirm) {
        ok.disabled = true; no.disabled = true;
        setState(confirm ? "stateRunning" : "stateRejected");
        confirmAction(actionId, confirm, "", card);
      }
      ok.addEventListener("click", function () {
        ok.disabled = true; no.disabled = true;
        setState("stateWaiting");
        fetch(API.smacPropose || "/platform/smac/propose", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ scenario: sc, kind: s.kind, params: s.params || {},
                                 description: (s.title || s.kind) + ": " + (s.reason || "") })
        })
          .then(readJson)
          .then(function (r) {
            if (!r.ok || !r.d || !r.d.id) {
              addMsg("agent", (t("failed") || "No se pudo: ") + ((r.d && (r.d.error || r.d.detail)) || "…"));
              ok.disabled = false; no.disabled = false;
              setState("stateFailed");
              return;
            }
            doConfirm(r.d.id, true);
          })
          .catch(function (err) {
            addMsg("agent", t("confirmFail") + (err && err.message ? " (" + err.message + ")" : ""));
            ok.disabled = false; no.disabled = false; setState("stateFailed");
          });
      });
      no.addEventListener("click", function () {
        /* Rechazar sin token: descarta y reanuda si estaba auto-pausada. */
        ok.disabled = true; no.disabled = true;
        fetch(API.smacResume || "/platform/smac/resume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin", body: JSON.stringify({ scenario: sc })
        })
          .then(function () { addMsg("agent", t("resumed")); })
          .catch(function () { addMsg("agent", t("confirmFail")); });
      });
      log.appendChild(card);
      log.scrollTop = log.scrollHeight;
    });
  }

  /* Banner de pausa automática con Reanudar (sin salir del chat). */
  var pauseShownFor = null;
  function maybeShowPause() {
    var ctx = window.NebulaAgent.ctx || {};
    var sc = ctx.scenario_id || currentScenario();
    if (!sc) return;
    if (ctx.auto_paused && pauseShownFor !== sc + ":paused") {
      pauseShownFor = sc + ":paused";
      if (window.NebulaAgent.notify) window.NebulaAgent.notify();
      addMsg("agent", "⏸ " + t("autoPaused"));
      var card = document.createElement("div");
      card.className = "nb-msg nb-msg--action";
      var btn = document.createElement("button");
      btn.className = "nb-action__confirm";
      btn.textContent = t("resumeBtn") || "Reanudar";
      btn.addEventListener("click", function () {
        btn.disabled = true;
        fetch(API.smacResume || "/platform/smac/resume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ scenario: sc })
        })
          .then(function () { addMsg("agent", t("resumed") || "Reanudado."); })
          .catch(function () { addMsg("agent", t("confirmFail")); });
      });
      card.appendChild(btn);
      log.appendChild(card);
      log.scrollTop = log.scrollHeight;
    } else if (!ctx.auto_paused) {
      pauseShownFor = null;
    }
  }

  /* ---------- puente UI ↔ agente (§5.2) ---------- */
  window.NebulaAgent = window.NebulaAgent || {};
  window.NebulaAgent.ctx = window.NebulaAgent.ctx || {};
  window.NebulaAgent.setContext = function (patch) {
    var prevStatus = (window.NebulaAgent.ctx || {}).scenario_status;
    window.NebulaAgent.ctx = Object.assign({}, window.NebulaAgent.ctx, patch || {});
    /* Cambio de escenario -> se carga su sección (o se empieza de 0). */
    if (patch && typeof patch.scenario_id === "string") switchScenario(patch.scenario_id);
    /* La barra de estado se repinta con cualquier parche: el panel publica
       su snapshot cada ~5 s, así el chat refleja el estado en vivo (§2.6). */
    if (patch) renderCtx();
    /* Sugerencias y pausa en el chat (sin salir al panel). */
    if (patch && (patch.suggestions !== undefined || patch.auto_paused !== undefined
        || patch.scenario_id)) {
      try { maybeShowSuggestions(); maybeShowPause(); } catch (e) {}
    }
    if (patch && patch.locale) {
      try { localStorage.setItem("nb_locale", patch.locale); } catch (e) {}
      /* Cambio de idioma con el chat abierto: se re-traduce el saludo
         (solo si aún no hay conversación) + chips + placeholder (§2.6). */
      var openMsgs = log.querySelectorAll(".nb-msg");
      if (openMsgs.length <= 1) {
        greeted = false;
        log.textContent = "";
        if (chat.dataset.open === "true") {
          greeted = true;
          greetedGeneric = !((window.NebulaAgent.ctx || {}).scenario_status);
          addMsg("agent", greet());
        }
      }
      renderChips();
      input.placeholder = t("placeholder");
      paintNewBtn();
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
  window.NebulaChat = { toggle: toggleChat, send: send, switchScenario: switchScenario, clearScenario: clearActiveSection };
  /* Botón "empezar de 0" + sección inicial según el escenario abierto. */
  var newBtn = chat.querySelector(".nb-chat__new");
  function paintNewBtn() {
    if (!newBtn) return;
    newBtn.setAttribute("aria-label", t("newChat"));
    newBtn.title = t("newChat");
  }
  if (newBtn) newBtn.addEventListener("click", clearActiveSection);
  paintNewBtn();
  switchScenario(currentScenario());
})();
