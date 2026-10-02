/* SMAC panel: live topology + control. El agente vive en el chat flotante. */
(function () {
    const scenarioEl = document.getElementById("smac-scenario");
    const loadBtn = document.getElementById("smac-load");
    const statusEl = document.getElementById("smac-status");
    const roundEl = document.getElementById("smac-round");
    const algoEl = document.getElementById("smac-algo");
    const countsEl = document.getElementById("smac-counts");
    const toggleBtn = document.getElementById("smac-refresh-toggle");
    const svg = document.getElementById("smac-topo");
    const selectedEl = document.getElementById("smac-selected");
    const peerEl = document.getElementById("smac-peer");
    const pendingEl = document.getElementById("smac-pending");
    const copyBtn = document.getElementById("smac-copy");
    const langBtn = document.getElementById("smac-lang");

    /* ---------- i18n ES/EN (§2.6: un solo idioma por sesión de UI) ---------- */
    const I18N = {
        es: {
            title: "Panel SMAC", subtitle: "observabilidad cognitiva y control interactivo",
            scenario: "Escenario", load: "Cargar", copyId: "Copiar id del escenario", copied: "¡Id copiado!",
            autoOn: "Actualización automática: sí", autoOff: "Actualización automática: no",
            autoOffDone: "Actualización automática: no (el escenario terminó)",
            analyze: "Analizar estado", analyzeResults: "Analizar resultados",
            reasoning: "Razonamiento cognitivo", control: "Control",
            pause: "Pausar nodo", resume: "Reanudar nodo", switchAgg: "Cambiar agregador",
            disconnect: "Proponer desconexión", stop: "Detener escenario",
            ask: "Preguntar / ordenar", askHint: "(lenguaje natural)",
            askNote: "Las respuestas aparecen en el chat del agente.",
            send: "Enviar", noNode: "ningún nodo seleccionado", nodeX: function (n) { return "nodo " + n; },
            stoppedReason: "El escenario terminó; no hay nodos que controlar",
            needSelect: "Selecciona un nodo en la topología",
            topoHint: "haz clic en un nodo para inspeccionar y controlar",
            round: "ronda", nodes: "nodos", active: "activos", pausedW: "en pausa", finishedW: "finalizados",
            lgRun: "en ejecución", lgPaused: "en pausa", lgFinished: "finalizado",
            lgOff: "offline/detenido", lgMal: "malicioso", lgStrag: "posible rezagado",
            isolated: "⚠ sin enlaces", roleTip: "Trainer + Aggregator",
            emptyFeed: "Carga un escenario para ver decisiones y explicaciones del agente.",
            emptyFeedDone: "Este escenario terminó sin intervenciones del agente.",
            noScenarios: "No hay escenarios todavía. Crea uno desde el Dashboard.",
            noScenarioId: "Escribe un id de escenario y pulsa Cargar.",
            historic: "histórico (config)",
            runtimeEmpty: "sin runtime: topología declarada",
            emptyNodes: "Sin nodos para este escenario.",
            emptyNodesSub: "Pulsa Cargar o crea uno desde el Dashboard.",
            loadError: "No se pudo leer el escenario. Reintentar",
            askPh: function (a, b) { return "p. ej. «¿por qué el nodo " + b + " es lento?» o «pausa el nodo " + a + "»"; },
            statusRunning: "en ejecución", statusCompleted: "✔ completado"
        },
        en: {
            title: "SMAC Panel", subtitle: "cognitive observability & interactive control",
            scenario: "Scenario", load: "Load", copyId: "Copy scenario id", copied: "Id copied!",
            autoOn: "Auto refresh: on", autoOff: "Auto refresh: off",
            autoOffDone: "Auto refresh: off (scenario finished)",
            analyze: "Analyze state", analyzeResults: "Analyze results",
            reasoning: "Cognitive reasoning", control: "Control",
            pause: "Pause node", resume: "Resume node", switchAgg: "Switch aggregator",
            disconnect: "Propose disconnect", stop: "Stop scenario",
            ask: "Ask / command", askHint: "(natural language)",
            askNote: "Replies appear in the agent chat.",
            send: "Send", noNode: "no node selected", nodeX: function (n) { return "node " + n; },
            stoppedReason: "The scenario finished; there are no nodes to control",
            needSelect: "Select a node in the topology",
            topoHint: "click a node to inspect & control",
            round: "round", nodes: "nodes", active: "running", pausedW: "paused", finishedW: "finished",
            lgRun: "running", lgPaused: "paused", lgFinished: "finished",
            lgOff: "offline/stopped", lgMal: "malicious", lgStrag: "predicted straggler",
            isolated: "⚠ no links", roleTip: "Trainer + Aggregator",
            emptyFeed: "Load a scenario to see agent decisions and explanations.",
            emptyFeedDone: "This scenario finished with no agent interventions.",
            noScenarios: "No scenarios yet. Create one from the Dashboard.",
            noScenarioId: "Type a scenario id and press Load.",
            historic: "historic (config)",
            runtimeEmpty: "no runtime: declared topology",
            emptyNodes: "No nodes for this scenario.",
            emptyNodesSub: "Press Load or create one from the Dashboard.",
            loadError: "Could not read the scenario. Retry",
            askPh: function (a, b) { return "e.g. \"why is node " + b + " slow?\" or \"pause node " + a + "\""; },
            statusRunning: "running", statusCompleted: "✔ completed"
        }
    };
    let lang = "en";
    try {
        lang = localStorage.getItem("nb_locale")
            || (((navigator.language || "en").toLowerCase().indexOf("es") === 0) ? "es" : "en");
    } catch (e) { lang = "en"; }
    function t(k) { return I18N[lang][k]; }
    function applyI18n() {
        document.querySelectorAll("[data-i18n]").forEach((el) => {
            const v = t(el.dataset.i18n);
            if (typeof v === "string") el.textContent = v;
        });
        document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
            const v = t(el.dataset.i18nAria);
            if (typeof v === "string") el.setAttribute("aria-label", v);
        });
        document.documentElement.lang = lang;
        if (langBtn) langBtn.textContent = lang === "es" ? "EN" : "ES";
        if (window.NebulaAgent) window.NebulaAgent.setContext({ locale: lang, page: location.pathname });
        if (state) render();
    }

    const NS = "http://www.w3.org/2000/svg";
    let state = null;
    let selected = null;
    let autoRefresh = true;
    let timer = null;

    /* Colores de estado = tokens --nb-* (§2.3). `finished` es un mapeo de
       visualización: escenario completed + nodo stopped/offline. */
    const STATE_COLORS = { running: "#2E9E6B", paused: "#D99A1B", finished: "#3E7CB1", offline: "#7C8199", stopped: "#7C8199" };

    function displayState(n) {
        const sc = ((state && state.status) || "").toLowerCase();
        if ((n.state === "stopped" || n.state === "offline") && sc === "completed") return "finished";
        return n.state;
    }
    function roundsTotal() {
        const r = state && state.config && state.config.rounds;
        const n = parseInt(r, 10);
        return Number.isFinite(n) ? n : null;
    }

    function scenario() { return scenarioEl.value.trim(); }

    /* Mensaje centrado en el lienzo (vacío / error): nunca un grafo en blanco. */
    function svgSize() {
        const W = (svg.clientWidth && svg.clientWidth > 50) ? svg.clientWidth : (svg.parentElement ? svg.parentElement.clientWidth : 600) || 600;
        const H = 480;
        try { svg.setAttribute("viewBox", "0 0 " + W + " " + H); } catch (e) {}
        return { W: W, H: H };
    }
    function drawMessage(text, sub) {
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        const size = svgSize(), W = size.W, H = size.H;
        const m = document.createElementNS(NS, "text");
        m.setAttribute("x", W / 2); m.setAttribute("y", sub ? H / 2 - 10 : H / 2);
        m.setAttribute("text-anchor", "middle"); m.setAttribute("fill", "#7C8199");
        m.setAttribute("font-size", "13");
        m.textContent = text;
        svg.appendChild(m);
        if (sub) {
            const s = document.createElementNS(NS, "text");
            s.setAttribute("x", W / 2); s.setAttribute("y", H / 2 + 14);
            s.setAttribute("text-anchor", "middle"); s.setAttribute("fill", "#ADB5BD");
            s.setAttribute("font-size", "11");
            s.textContent = sub;
            svg.appendChild(s);
        }
    }

    /* Sin escenario en la URL: cargar el más reciente (o el que esté en
       ejecución) para que la topología nunca arranque vacía. */
    async function autoPickScenario() {
        if (scenario()) return true;
        let res;
        try {
            res = await fetch("/platform/api/scenarios", { credentials: "same-origin" });
        } catch (e) {
            setStatus(t("loadError"));
            drawMessage(t("loadError"), String(e && e.message ? e.message : e));
            return false;
        }
        if (!res.ok) {
            if (res.status === 401) {
                setStatus("login required");
                drawMessage("login required", t("noScenarioId"));
                return false;
            }
            setStatus(t("loadError"));
            drawMessage(t("loadError") + " (" + res.status + ")", t("noScenarioId"));
            return false;
        }
        let data;
        try {
            data = await res.json();
        } catch (e) {
            setStatus(t("loadError"));
            drawMessage(t("loadError"), "invalid JSON");
            return false;
        }
        // El backend puede devolver lista directa o {scenarios, scenario_running}.
        const list = Array.isArray(data) ? data : (data.scenarios || data.items || []);
        const running = (!Array.isArray(data) && data.scenario_running) || null;
        if (!list.length && !running) {
            setStatus("—");
            drawMessage(t("noScenarios"));
            return false;
        }
        try {
            const parseTs = (s) => {
                const m = /^(\d+)\/(\d+)\/(\d+) (\d+):(\d+):(\d+)/.exec(s || "");
                return m ? new Date(m[3], m[2] - 1, m[1], m[4], m[5], m[6]).getTime() : 0;
            };
            let pick = running;
            if (!pick && list.length) {
                const sorted = list.slice().sort((a, b) => parseTs(b.start_time || b.startTime) - parseTs(a.start_time || a.startTime));
                const first = sorted[0];
                pick = (first && (first.name || first.id || first.scenario)) || null;
            }
            if (!pick) {
                setStatus("—");
                drawMessage(t("noScenarios"));
                return false;
            }
            scenarioEl.value = pick;
            scenarioEl.title = pick;
            return true;
        } catch (e) {
            setStatus(t("loadError"));
            drawMessage(t("loadError"), String(e && e.message ? e.message : e));
            return false;
        }
    }

    async function fetchState() {
        const sc = scenario();
        if (!sc) {
            setStatus("—");
            drawMessage(t("noScenarioId"), t("noScenarios"));
            return;
        }
        let res;
        try {
            res = await fetch("/platform/smac/state?scenario=" + encodeURIComponent(sc), { credentials: "same-origin" });
        } catch (e) {
            setStatus(t("loadError"));
            drawMessage(t("loadError"), String(e && e.message ? e.message : e));
            return;
        }
        if (res.status === 401) {
            setStatus("login required");
            drawMessage("login required", t("noScenarioId"));
            return;
        }
        if (!res.ok) {
            setStatus(t("loadError"));
            let detail = "";
            try { detail = (await res.text()).slice(0, 200); } catch (e) {}
            drawMessage(t("loadError") + " (" + res.status + ")", detail);
            return;
        }
        try {
            state = await res.json();
        } catch (e) {
            setStatus(t("loadError"));
            drawMessage(t("loadError"), "invalid JSON");
            return;
        }
        render();
    }

    function setStatus(text, cls) {
        statusEl.textContent = text;
        statusEl.className = "nb-badge" + (cls ? " " + cls : "");
    }

    function render() {
        if (!state) return;
        const st = ((state.status || "unknown") + "").toLowerCase();
        const isRunning = st === "running";
        const isDone = st === "completed" || st === "finished";
        statusEl.textContent = isDone ? t("statusCompleted") : (isRunning ? t("statusRunning") : (state.status || "unknown"));
        statusEl.className = "nb-badge" + (isDone ? " nb-badge--ok" : (isRunning ? "" : ""));
        const total = roundsTotal();
        const cur = (state.round === null || state.round === undefined) ? "—" : state.round;
        roundEl.textContent = t("round") + " " + cur + (total ? "/" + total : "");
        algoEl.textContent = (state.config && state.config.aggregation) || "—";
        const nodes = Array.isArray(state.nodes) ? state.nodes : [];
        const edges = Array.isArray(state.edges) ? state.edges : [];
        const stragglers = Array.isArray(state.predicted_stragglers) ? state.predicted_stragglers : [];
        const counts = { running: 0, paused: 0, finished: 0, off: 0 };
        nodes.forEach((n) => {
            const ds = displayState(n || {});
            if (ds === "running") counts.running++;
            else if (ds === "paused") counts.paused++;
            else if (ds === "finished") counts.finished++;
            else counts.off++;
        });
        const isFallback = !!(state.fallback || (state.runtime && state.runtime.fallback));
        const isEmpty = !!(state.runtime_empty || (state.runtime && state.runtime.empty));
        countsEl.textContent = `${nodes.length} ${t("nodes")} · ${counts.running} ${t("active")} · ${counts.paused} ${t("pausedW")} · ${counts.finished} ${t("finishedW")}` +
            (counts.off ? ` · ${counts.off} offline` : "") +
            (stragglers.length ? ` · 🟠${t("lgStrag")}: ${stragglers.join(",")}` : "") +
            (isFallback ? ` · ${t("historic")}` : (isEmpty ? ` · ${t("runtimeEmpty")}` : ""));
        toggleBtn.textContent = autoRefresh ? t("autoOn") : (isDone ? t("autoOffDone") : t("autoOff"));
        toggleBtn.setAttribute("aria-checked", String(autoRefresh));
        toggleBtn.disabled = !isRunning && !scenario();
        const hintEl = document.getElementById("smac-topo-hint");
        if (hintEl) hintEl.textContent = isFallback ? (t("topoHint") + " · " + t("historic")) : t("topoHint");
        if (window.NebulaAgent) window.NebulaAgent.setContext({
            page: location.pathname,
            scenario_id: scenario(), scenario_status: st,
            round_visible: state.round, rounds: total,
            aggregation: (state.config && state.config.aggregation) || "",
            nodes_total: nodes.length,
            nodes_running: counts.running, nodes_paused: counts.paused,
            nodes_finished: counts.finished, nodes_offline: counts.off,
            stragglers: stragglers.map((n) => n),
            malicious: nodes.filter((n) => n && n.malicious).map((n) => n.idx),
            historic: isFallback, runtime_empty: isEmpty,
            selected_node: selected, node_ids: nodes.map((n) => n && n.idx)
        });
        renderTopo();
        renderPeers();
        updateControls();
    }

    function renderTopo() {
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        const nodes = Array.isArray(state.nodes) ? state.nodes : [];
        const edges = Array.isArray(state.edges) ? state.edges : [];
        const stragglers = Array.isArray(state.predicted_stragglers) ? state.predicted_stragglers : [];
        const size = svgSize(), W = size.W, H = size.H, cx = W / 2, cy = H / 2;
        const R = Math.max(60, Math.min(W, H) / 2 - 60);
        const pos = {};
        nodes.forEach((n, i) => {
            if (!n) return;
            const a = (2 * Math.PI * i) / Math.max(1, nodes.length) - Math.PI / 2;
            pos[String(n.idx)] = { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) };
        });
        const norm = (v) => (v === null || v === undefined) ? "" : String(v);
        const selKey = selected !== null && selected !== undefined ? norm(selected) : null;
        const selNeighbors = selKey !== null ? new Set([selKey]) : null;
        if (selKey !== null) {
            edges.forEach((e) => {
                const a = norm(e.a), b = norm(e.b);
                if (a === selKey) selNeighbors.add(b);
                if (b === selKey) selNeighbors.add(a);
            });
        }
        const isFallbackTopo = !!(state.fallback || (state.runtime && state.runtime.fallback));
        edges.forEach((e) => {
            const a = norm(e.a), b = norm(e.b);
            if (!pos[a] || !pos[b]) return;
            const line = document.createElementNS(NS, "line");
            line.setAttribute("x1", pos[a].x); line.setAttribute("y1", pos[a].y);
            line.setAttribute("x2", pos[b].x); line.setAttribute("y2", pos[b].y);
            line.setAttribute("stroke", "#adb5bd"); line.setAttribute("stroke-width", "1.5");
            if (isFallbackTopo) line.setAttribute("stroke-dasharray", "5 4");
            line.setAttribute("class", "smac-edge" + (selNeighbors && !(selNeighbors.has(a) && selNeighbors.has(b)) ? " dim" : ""));
            svg.appendChild(line);
        });
        nodes.forEach((n) => {
            if (!n) return;
            const key = norm(n.idx);
            const p = pos[key] || pos[n.idx];
            if (!p) return;
            const g = document.createElementNS(NS, "g");
            g.setAttribute("class", "smac-node" + (selNeighbors && !selNeighbors.has(key) ? " dim" : ""));
            g.addEventListener("click", () => select(key));
            if (stragglers.map(norm).includes(key)) {
                const ring = document.createElementNS(NS, "circle");
                ring.setAttribute("cx", p.x); ring.setAttribute("cy", p.y); ring.setAttribute("r", 24);
                ring.setAttribute("fill", "none"); ring.setAttribute("stroke", "#fd7e14"); ring.setAttribute("stroke-width", "3");
                g.appendChild(ring);
            }
            const c = document.createElementNS(NS, "circle");
            c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", 18);
            const ds = displayState(n);
            c.setAttribute("fill", STATE_COLORS[ds] || "#7C8199");
            if (n.fallback) { c.setAttribute("fill-opacity", "0.85"); c.setAttribute("stroke-dasharray", "4 3"); }
            if (ds === "finished") {
                /* Anillo azul + check: terminado bien, no caído (D4) */
                const ring = document.createElementNS(NS, "circle");
                ring.setAttribute("cx", p.x); ring.setAttribute("cy", p.y); ring.setAttribute("r", 23);
                ring.setAttribute("fill", "none"); ring.setAttribute("stroke", "#3E7CB1"); ring.setAttribute("stroke-width", "2");
                g.appendChild(ring);
            }
            if (n.malicious) { c.setAttribute("stroke", "#D64545"); c.setAttribute("stroke-width", "3"); }
            if (selKey !== null && selKey === key) { c.setAttribute("stroke", "#FF8A65"); c.setAttribute("stroke-width", "3"); }
            g.appendChild(c);
            const idxText = document.createElementNS(NS, "text");
            idxText.setAttribute("x", p.x); idxText.setAttribute("y", p.y + 5);
            idxText.setAttribute("text-anchor", "middle"); idxText.setAttribute("fill", "white");
            idxText.setAttribute("font-size", "13"); idxText.setAttribute("font-weight", "bold");
            idxText.textContent = n.idx;
            g.appendChild(idxText);
            const label = document.createElementNS(NS, "text");
            label.setAttribute("x", p.x); label.setAttribute("y", p.y + 36);
            label.setAttribute("text-anchor", "middle"); label.setAttribute("font-size", "10"); label.setAttribute("fill", "#3A3F55");
            label.style.cursor = "help";
            label.textContent = (n.role || "").replace("trainer_aggregator", "T+A") + (n.container_state === "paused" ? " ⏸" : "");
            const roleTip = document.createElementNS(NS, "title");
            roleTip.textContent = t("roleTip");
            label.appendChild(roleTip);
            g.appendChild(label);
            const linked = edges.some((e) => norm(e.a) === key || norm(e.b) === key);
            if (!linked && nodes.length > 1) {
                /* Nodo aislado: aviso visible, no solo ausencia de aristas (D4) */
                const iso = document.createElementNS(NS, "text");
                iso.setAttribute("x", p.x); iso.setAttribute("y", p.y + 48);
                iso.setAttribute("text-anchor", "middle"); iso.setAttribute("font-size", "9"); iso.setAttribute("fill", "#D99A1B");
                iso.textContent = t("isolated");
                g.appendChild(iso);
            }
            if (n.fallback) {
                const hist = document.createElementNS(NS, "text");
                hist.setAttribute("x", p.x); hist.setAttribute("y", p.y + 48);
                hist.setAttribute("text-anchor", "middle"); hist.setAttribute("font-size", "9"); hist.setAttribute("fill", "#7C8199");
                hist.textContent = t("historic");
                g.appendChild(hist);
            }
            const title = document.createElementNS(NS, "title");
            const ds2 = displayState(n);
            title.textContent = `${t("nodeX")(key)} · ${ds2}${n.malicious ? " · MALICIOUS" : ""} · ${t("round")} ${(n.round === null || n.round === undefined) ? "—" : n.round} · ${t("roleTip")}${n.fallback ? " · " + t("historic") : ""}`;
            g.appendChild(title);
            svg.appendChild(g);
        });
        if (!nodes.length) {
            // Nunca blanco: mensaje explícito con pista de acción.
            drawMessage(t("emptyNodes"), t("emptyNodesSub"));
        } else if (isFallbackTopo) {
            const banner = document.createElementNS(NS, "text");
            banner.setAttribute("x", cx); banner.setAttribute("y", 18);
            banner.setAttribute("text-anchor", "middle"); banner.setAttribute("font-size", "11"); banner.setAttribute("fill", "#7C8199");
            banner.textContent = t("runtimeEmpty") + " · " + t("historic");
            svg.appendChild(banner);
        }
    }

    function renderPeers() {
        peerEl.innerHTML = '<option value="">peer…</option>';
        const nodeList = Array.isArray(state.nodes) ? state.nodes : [];
        const selKey2 = selected !== null && selected !== undefined ? String(selected) : null;
        const sel = selKey2 !== null ? nodeList.find((n) => n && String(n.idx) === selKey2) : null;
        const candidates = sel ? (sel.neighbors || []) : nodeList.map((n) => n.idx);
        candidates.forEach((nb) => {
            const opt = document.createElement("option");
            opt.value = nb;
            opt.textContent = "node " + nb;
            peerEl.appendChild(opt);
        });
    }

    function updateControls() {
        /* §2.4: si no está running, todo deshabilitado con motivo (D5) */
        const isRunning = ((state && state.status) || "").toLowerCase() === "running";
        const hasSel = !!selected;
        const pauseBtn = document.getElementById("smac-pause");
        const resumeBtn = document.getElementById("smac-resume");
        const applyBtn = document.getElementById("smac-apply-algo");
        const algoSel = document.getElementById("smac-algo-select");
        const discBtn = document.getElementById("smac-disconnect");
        const stopBtn = document.getElementById("smac-stop");
        pauseBtn.disabled = !(isRunning && hasSel);
        resumeBtn.disabled = !(isRunning && hasSel);
        pauseBtn.title = resumeBtn.title = !isRunning ? t("stoppedReason") : (hasSel ? "" : t("needSelect"));
        applyBtn.disabled = !isRunning;
        algoSel.disabled = !isRunning;
        applyBtn.title = algoSel.title = !isRunning ? t("stoppedReason") : "";
        peerEl.disabled = !(isRunning && hasSel);
        discBtn.disabled = !(isRunning && hasSel);
        peerEl.title = discBtn.title = !isRunning ? t("stoppedReason") : (hasSel ? "" : t("needSelect"));
        stopBtn.disabled = !isRunning;
        stopBtn.title = !isRunning ? t("stoppedReason") : "";
        selectedEl.textContent = hasSel ? t("nodeX")(selected) : t("noNode");
    }

    function select(idx) {
        const key = (idx === null || idx === undefined) ? null : String(idx);
        const cur = (selected === null || selected === undefined) ? null : String(selected);
        selected = (cur === key) ? null : key;
        if (window.NebulaAgent) window.NebulaAgent.setContext({ selected_node: selected });
        renderTopo();
        renderPeers();
        updateControls();
    }

    function pendingCard(action) {
        const box = document.createElement("div");
        box.className = "smac-card";
        box.style.background = "#fff3cd";
        const desc = document.createElement("div");
        desc.textContent = "⚙️ " + action.kind + ": " + action.description;
        box.appendChild(desc);
        let nameInput = null;
        if (action.kind === "stop_scenario") {
            nameInput = document.createElement("input");
            nameInput.className = "form-control form-control-sm mt-1";
            nameInput.placeholder = "Type the scenario name to enable confirm";
            box.appendChild(nameInput);
        }
        const row = document.createElement("div");
        row.className = "mt-1";
        const ok = document.createElement("button");
        ok.className = "btn btn-sm btn-warning me-2";
        ok.textContent = "Confirm & run";
        const no = document.createElement("button");
        no.className = "btn btn-sm btn-outline-secondary";
        no.textContent = "Discard";
        ok.onclick = () => confirmAction(action.id, true, box, ok, no, nameInput ? nameInput.value : null);
        no.onclick = () => confirmAction(action.id, false, box, ok, no, null);
        row.appendChild(ok); row.appendChild(no);
        box.appendChild(row);
        pendingEl.appendChild(box);
    }

    async function confirmAction(id, confirm, box, ok, no, confirmName) {
        ok.disabled = true; no.disabled = true;
        try {
            const res = await fetch("/platform/smac/chat/confirm", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: id, confirm: confirm, confirm_name: confirmName }),
            });
            const data = await res.json();
            const note = document.createElement("div");
            note.className = "mt-1 small";
            note.textContent = (data.ok ? "✅ " : "❌ ") + (data.detail || JSON.stringify(data));
            box.appendChild(note);
            if (data.ok) fetchState();
        } catch (e) {
            const note = document.createElement("div");
            note.className = "mt-1 small text-danger";
            note.textContent = "Request failed: " + e;
            box.appendChild(note);
        }
    }

    async function propose(kind, params, description) {
        if (!scenario()) { alert("Load a scenario first."); return; }
        try {
            const res = await fetch("/platform/smac/propose", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scenario: scenario(), kind: kind, params: params, description: description }),
            });
            if (res.status === 401) { alert("Session expired. Log in again."); return; }
            const action = await res.json();
            pendingCard(action);
        } catch (e) { alert("Propose failed: " + e); }
    }

    document.getElementById("smac-pause").onclick = () =>
        propose("pause_node", { node: selected }, `Pause node ${selected}`);
    document.getElementById("smac-resume").onclick = () =>
        propose("resume_node", { node: selected }, `Resume node ${selected}`);
    document.getElementById("smac-apply-algo").onclick = () => {
        const algo = document.getElementById("smac-algo-select").value;
        const target = selected || "all";
        propose("select_aggregator", { node: target, algorithm: algo }, `Switch aggregation to ${algo} on ${target}`);
    };
    document.getElementById("smac-disconnect").onclick = () => {
        if (!peerEl.value) { alert("Choose a peer first."); return; }
        propose("propose_disconnect", { node: selected, peer: peerEl.value },
            `Propose node ${selected} disconnects from ${peerEl.value}`);
    };
    document.getElementById("smac-stop").onclick = () =>
        propose("stop_scenario", {}, `Stop scenario ${scenario()}`);

    async function load() {
        selected = null;
        pendingEl.innerHTML = "";
        if (await autoPickScenario()) fetchState();
    }
    loadBtn.onclick = async () => { await load(); startEvents(); };

    let eventSource = null;
    function startEvents() {
        if (eventSource) { try { eventSource.close(); } catch (e) { /* noop */ } }
        if (!scenario() || typeof EventSource === "undefined") return;
        eventSource = new EventSource("/platform/api/smac/events?scenario=" + encodeURIComponent(scenario()));
        /* Sin tarjeta de reasoning: los eventos solo refrescan el estado y
           avisan con el punto del chat flotante. */
        ["predicted_straggler", "node_offline", "node_back", "aggregator_switched",
         "scenario_status", "scenario_finished"].forEach((kind) => {
            eventSource.addEventListener(kind, (ev) => {
                try { JSON.parse(ev.data); } catch (e) { console.warn("SMAC SSE parse error", e); return; }
                if (window.NebulaAgent) window.NebulaAgent.notify();
                fetchState();
            });
        });
        eventSource.onerror = () => { /* browser retries automatically */ };
    }

    toggleBtn.onclick = () => {
        autoRefresh = !autoRefresh;
        toggleBtn.textContent = autoRefresh ? t("autoOn") : t("autoOff");
        toggleBtn.setAttribute("aria-checked", String(autoRefresh));
        restartTimer();
    };
    if (copyBtn) copyBtn.onclick = async () => {
        const id = scenario();
        if (!id) return;
        try { await navigator.clipboard.writeText(id); } catch (e) {
            const tmp = document.createElement("input");
            tmp.value = id; document.body.appendChild(tmp); tmp.select();
            try { document.execCommand("copy"); } catch (e2) {}
            tmp.remove();
        }
        copyBtn.textContent = "✓";
        setTimeout(() => { copyBtn.textContent = "⧉"; }, 1200);
    };
    if (langBtn) langBtn.onclick = () => {
        lang = (lang === "es") ? "en" : "es";
        try { localStorage.setItem("nb_locale", lang); } catch (e) {}
        applyI18n();
    };
    function restartTimer() {
        if (timer) clearInterval(timer);
        if (autoRefresh) timer = setInterval(() => { if (scenario()) fetchState(); }, 5000);
    }
    // Re-medimos al cambiar el tamaño: el SVG usa viewBox dinámico y el
    // anillo se recalcula; sin esto, un card colapsado al inicio dejaba R=60
    // para siempre y parecía "vacío".
    let resizeScheduled = false;
    function scheduleRerender() {
        if (resizeScheduled) return;
        resizeScheduled = true;
        requestAnimationFrame(() => {
            resizeScheduled = false;
            try {
                if (state) renderTopo();
                else svgSize();
            } catch (e) {}
        });
    }
    try {
        if (typeof ResizeObserver !== "undefined" && svg.parentElement) {
            new ResizeObserver(scheduleRerender).observe(svg.parentElement);
        }
        window.addEventListener("resize", scheduleRerender);
    } catch (e) {}
    restartTimer();
    applyI18n();
    // Mensaje inmediato para no mostrar nunca el SVG en blanco antes del fetch.
    drawMessage(t("noScenarioId"), t("noScenarios"));
    (async () => { await load(); startEvents(); })();
})();
