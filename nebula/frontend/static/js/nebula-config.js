/* NEBULA · Endpoints y configuración compartida del frontend.
   Fuente única de rutas API y de límites/tiempos (inyectados por el backend
   en `window.NebulaConfig`; los valores aquí son solo fallback). */
(function () {
  "use strict";

  var cfg = window.NebulaConfig || {};
  window.NebulaConfig = Object.assign(
    {
      pollMs: 5000,
      copyResetMs: 1200,
      histMaxMsg: 60,
      histMaxSc: 15,
      histMaxLen: 2000,
      sseMaxWaitS: 90,
      ssePollS: 4,
      breakpointDock: 1280,
      chipsMax: 3,
      chatReadyResetMs: 5000,
      monitorPollMs: 5000,
      monitorQueueMs: 100,
      monitorStaleBaseMs: 20000,
      monitorStalePerNodeMs: 500,
      topoHeightPx: 480,
      firebase: {},
      gtagId: "",
    },
    cfg
  );

  function esc(s) {
    return encodeURIComponent(s);
  }

  window.NebulaApi = {
    scenarios: "/platform/api/scenarios",
    scenarioSummary: function (sc) {
      return "/platform/api/scenarios/" + esc(sc) + "/summary";
    },
    scenarioSeries: function (sc, metric) {
      return "/platform/api/scenarios/" + esc(sc) + "/metrics?metric=" + esc(metric);
    },
    scenarioEvents: function (sc) {
      return "/platform/api/smac/events?scenario=" + esc(sc);
    },
    smacState: function (sc) {
      return "/platform/smac/state?scenario=" + esc(sc);
    },
    smacChat: "/platform/smac/chat/",
    smacConfirm: "/platform/smac/chat/confirm",
    smacPropose: "/platform/smac/propose",
    smacResume: "/platform/smac/resume",
    dashboardStatistics: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/statistics/";
    },
    // §5.1: dashboard/monitor/deployment/auth (antes literales en cada módulo).
    dashboardConfig: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/config";
    },
    dashboardNotes: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/notes";
    },
    dashboardSaveNote: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/save_note";
    },
    dashboardRelaunch: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/relaunch";
    },
    dashboardRemove: function (sc) {
      return "/platform/dashboard/" + esc(sc) + "/remove";
    },
    dashboardMonitor: function (sc) {
      return "/platform/api/dashboard/" + esc(sc) + "/monitor";
    },
    nodeMetrics: function (sc, hash) {
      return "/platform/dashboard/" + esc(sc) + "/node/" + esc(hash) + "/metrics";
    },
    nodeLog: function (sc, idx, kind) {
      return "/platform/dashboard/" + esc(sc) + "/node/" + esc(idx) + "/" + esc(kind);
    },
    deploymentRun: "/platform/dashboard/deployment/run",
    discoverVpn: "/platform/api/discover-vpn",
    firebaseAuth: "/platform/firebase_auth",
    login: "/platform/login",
    staticImage: function (name) {
      return "/platform/static/images/" + String(name).replace(/^\/+/, "");
    },
  };
})();
