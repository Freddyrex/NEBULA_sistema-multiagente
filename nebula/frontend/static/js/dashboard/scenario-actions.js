// Scenario Actions Module (Bootstrap 5 native API — no jQuery .modal())
const ScenarioActions = {
    init() {
        this.bindEvents();
    },

    // --- Bootstrap 5 helpers (con fallback a jQuery si existiera plugin viejo) ---
    _getModalEl(id) {
        return document.getElementById(id);
    },

    _showModal(id) {
        const el = this._getModalEl(id);
        if (!el) return;
        if (window.bootstrap && window.bootstrap.Modal) {
            window.bootstrap.Modal.getOrCreateInstance(el).show();
        } else if (window.jQuery && typeof window.jQuery(el).modal === 'function') {
            window.jQuery(el).modal('show');
        } else {
            el.classList.add('show');
            el.style.display = 'block';
        }
    },

    _hideModal(id) {
        const el = this._getModalEl(id);
        if (!el) return;
        if (window.bootstrap && window.bootstrap.Modal) {
            const instance = window.bootstrap.Modal.getInstance(el)
                || window.bootstrap.Modal.getOrCreateInstance(el);
            instance.hide();
        } else if (window.jQuery && typeof window.jQuery(el).modal === 'function') {
            window.jQuery(el).modal('hide');
        } else {
            el.classList.remove('show');
            el.style.display = 'none';
        }
        // Limpieza de seguridad: si no queda ningún modal abierto,
        // eliminar backdrops huérfanos que dejan la pantalla opaca y bloquean clics.
        setTimeout(() => {
            const anyOpen = document.querySelector('.modal.show');
            if (!anyOpen) {
                document.querySelectorAll('.modal-backdrop').forEach((b) => b.remove());
                document.body.classList.remove('modal-open');
                document.body.style.overflow = '';
                document.body.style.paddingRight = '';
            }
        }, 350);
    },

    _showInfo(message) {
        const body = document.getElementById('info-modal-body');
        if (body) body.innerHTML = message;
        this._showModal('info-modal');
    },

    // Muestra info-modal solo cuando confirm-modal ya terminó de cerrarse,
    // para no apilar dos backdrops (pantalla opaca + botones muertos).
    _showInfoAfterConfirmHidden(message) {
        const confirmEl = this._getModalEl('confirm-modal');
        const isOpen = confirmEl && confirmEl.classList.contains('show');
        if (confirmEl && isOpen) {
            confirmEl.addEventListener('hidden.bs.modal', () => {
                this._showInfo(message);
            }, { once: true });
            this._hideModal('confirm-modal');
        } else {
            this._hideModal('confirm-modal');
            // Pequeño retardo para dejar que el backdrop salga antes de abrir el siguiente modal.
            setTimeout(() => this._showInfo(message), 150);
        }
    },

    bindEvents() {
        // Delegación: soporta la clase actual y el id antiguo por compatibilidad.
        $(document).on('click', '#relaunch-btn, .js-relaunch-btn', this.handleRelaunch.bind(this));
        $(document).on('click', '#remove-btn, .js-remove-btn', this.handleRemove.bind(this));
    },

    handleRelaunch(event) {
        const trigger = $(event.currentTarget);
        const scenarioName = trigger.data('scenario-name');
        const scenarioTitle = trigger.data('scenario-title');

        const modalEl = this._getModalEl('confirm-modal');
        modalEl.querySelector('.modal-title').textContent = 'Relaunch scenario';
        document.getElementById('confirm-modal-body').innerHTML =
            `Are you sure you want to relaunch the scenario ${scenarioTitle}?`;
        this._showModal('confirm-modal');

        document.getElementById('yes-button').onclick = () => {
            this._hideModal('confirm-modal');
            this.executeRelaunch(scenarioName);
        };
    },

    handleRemove(event) {
        const trigger = $(event.currentTarget);
        const scenarioName = trigger.data('scenario-name');

        const modalEl = this._getModalEl('confirm-modal');
        modalEl.querySelector('.modal-title').textContent = 'Remove scenario';
        document.getElementById('confirm-modal-body').innerHTML =
            `Are you sure you want to remove the scenario ${scenarioName}?<br><br>` +
            `<p class="badge text-bg-danger">Warning: you will remove the scenario from the database</p>`;
        this._showModal('confirm-modal');

        document.getElementById('yes-button').onclick = () => {
            this._hideModal('confirm-modal');
            this.executeRemove(scenarioName);
        };
    },

    async executeRelaunch(scenarioName) {
        try {
            const response = await fetch(`/platform/dashboard/${scenarioName}/relaunch`, {
                method: 'GET'
            });

            if (response.redirected) {
                window.location.href = response.url;
            } else {
                this._showInfoAfterConfirmHidden(
                    'You are not allowed to relaunch a scenario with demo role.'
                );
            }
        } catch (error) {
            console.error('Error:', error);
            this._showInfoAfterConfirmHidden(
                'An unexpected error occurred while relaunching the scenario.'
            );
        }
    },

    async executeRemove(scenarioName) {
        try {
            const response = await fetch(`/platform/dashboard/${scenarioName}/remove`, {
                method: 'GET'
            });

            if (response.redirected) {
                window.location.href = response.url;
            } else {
                this._showInfoAfterConfirmHidden(
                    'You are not allowed to remove a scenario with demo role.'
                );
            }
        } catch (error) {
            console.error('Error:', error);
            this._showInfoAfterConfirmHidden(
                'An unexpected error occurred while removing the scenario.'
            );
        }
    }
};

export default ScenarioActions;
