// Adaptive Agent (SMAC copilot) Module
// Serializes the `adaptive_agent` block of scenario.json. Every sub-flag is
// OFF by default; `pause_on_suggestion` defaults to true only when the master
// switch is on (an enabled agent that never pauses would never surface its
// suggestions to the operator).
const AdaptiveAgentManager = (function () {
    const DEFAULT_CONFIG = {
        multi_agent: false,
        exec_interpreter: false,
        aggregator_suggestions: false,
        architecture_suggestions: false,
        // null = derive from the master switch (true when enabled); explicit
        // true/false wins so a saved scenario round-trips faithfully.
        pause_on_suggestion: null,
        aggregator_auto: false
    };

    function initializeAdaptiveAgent() {
        setupMasterSwitch();
        setAdaptiveAgentConfig(DEFAULT_CONFIG);
    }

    function setupMasterSwitch() {
        const master = document.getElementById("adaptiveAgentSwitch");
        if (!master) return;
        master.addEventListener("change", function () {
            toggleAdaptiveAgentSettings(this.checked);
        });
    }

    function toggleAdaptiveAgentSettings(enabled) {
        const settings = document.getElementById("adaptive-agent-settings");
        if (!settings) return;
        settings.style.display = enabled ? "block" : "none";
    }

    function getAdaptiveAgentConfig() {
        const enabled = Boolean(document.getElementById("adaptiveAgentSwitch")?.checked);
        if (!enabled) {
            // Master OFF collapses every flag to false so scenario.json stays
            // clean and backward-compatible.
            return {
                multi_agent: false,
                exec_interpreter: false,
                aggregator_suggestions: false,
                architecture_suggestions: false,
                pause_on_suggestion: false,
                aggregator_auto: false
            };
        }
        const pauseSwitch = document.getElementById("adaptivePauseSwitch");
        return {
            multi_agent: Boolean(document.getElementById("adaptiveMultiAgentSwitch")?.checked),
            exec_interpreter: Boolean(document.getElementById("adaptiveExecInterpreterSwitch")?.checked),
            aggregator_suggestions: Boolean(document.getElementById("adaptiveAggregatorSwitch")?.checked),
            architecture_suggestions: Boolean(document.getElementById("adaptiveArchitectureSwitch")?.checked),
            pause_on_suggestion: pauseSwitch ? Boolean(pauseSwitch.checked) : true,
            // Reserved (Sprint 7): the server never auto-executes even when true.
            aggregator_auto: Boolean(document.getElementById("adaptiveAggregatorAutoSwitch")?.checked)
        };
    }

    function setAdaptiveAgentConfig(config = DEFAULT_CONFIG) {
        const cfg = { ...DEFAULT_CONFIG, ...(config || {}) };
        const enabled = Boolean(
            cfg.multi_agent || cfg.exec_interpreter || cfg.aggregator_suggestions ||
            cfg.architecture_suggestions || cfg.pause_on_suggestion
        );

        const master = document.getElementById("adaptiveAgentSwitch");
        if (!master) return;
        master.checked = enabled;

        const setChecked = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.checked = Boolean(value);
        };
        setChecked("adaptiveMultiAgentSwitch", cfg.multi_agent);
        setChecked("adaptiveExecInterpreterSwitch", cfg.exec_interpreter);
        setChecked("adaptiveAggregatorSwitch", cfg.aggregator_suggestions);
        setChecked("adaptiveArchitectureSwitch", cfg.architecture_suggestions);
        // Absent key (old scenarios) → default true when the master is on.
        setChecked("adaptivePauseSwitch",
            cfg.pause_on_suggestion === null || cfg.pause_on_suggestion === undefined
                ? true
                : cfg.pause_on_suggestion);
        setChecked("adaptiveAggregatorAutoSwitch", cfg.aggregator_auto);

        toggleAdaptiveAgentSettings(enabled);
    }

    function resetAdaptiveAgentConfig() {
        setAdaptiveAgentConfig(DEFAULT_CONFIG);
    }

    return {
        initializeAdaptiveAgent,
        getAdaptiveAgentConfig,
        setAdaptiveAgentConfig,
        resetAdaptiveAgentConfig
    };
})();

export default AdaptiveAgentManager;
