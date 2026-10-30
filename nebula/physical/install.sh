#!/usr/bin/env bash
###############################################################################
# Provision *once* (or update safely) a Raspberry Pi node that will:
#
#   • run inside a DietPi / Debian 12 “Bookworm” ARM64 image
#   • register itself in the static node registry (NEBULA_NODES)
#   • clone the *Nebula-DFL* repository (or update it if already there)
#   • create a per-project Python 3.11 virtual-env with **uv**
#
# Every section is **idempotent**: re-running the script will skip work that is
# already done and only apply missing pieces.
#
# Usage ────────────────────────────────────────────────────────────────────────
#   NEBULA_NODES='{"pi1":"192.168.1.11:8000","pi2":"192.168.1.12:8000"}' \
#     ./idempotent_node.sh               # run locally on the Pi (as dietpi user)
#
###############################################################################
set -euo pipefail

###############################################################################
# USER CONFIGURATION
###############################################################################
AUTH_KEY="${TAILSCALE_AUTH_KEY:?Set TAILSCALE_AUTH_KEY in the environment (never hardcode it)}"
REPO_URL="https://github.com/CyberDataLab/nebula.git"
REPO_BRANCH="physical-deployment"
PY_VERSION="3.11.7"               # exact CPython build to install with `uv`
VENV_DIR=".venv"                  # venv folder inside the repo
###############################################################################

sudo -v  # cache sudo credentials once

###############################################################################
# 0 ▸ TIME & CLOCK ────────────────────────────────────────────────────────────
# Synchronise the RTC/NTP early to avoid “Release file … not valid yet” apt
# errors when the Pi’s clock is far in the past.
###############################################################################
echo "· Syncing system clock with NTP …"
sudo timedatectl set-ntp true
sudo timedatectl set-timezone "${NEBULA_TZ:-Etc/UTC}"
for _ in {1..15}; do
  timedatectl | grep -q "System clock synchronized: yes" && break
  sleep 1
done

###############################################################################
# 1 ▸ APT SOURCE CLEAN-UP ─────────────────────────────────────────────────────
# DietPi sometimes duplicates entries; remove known offenders so that
# `apt-get update` stays quiet.
###############################################################################
sudo rm -f /etc/apt/sources.list.d/backports.list

###############################################################################
# 2 ▸ BASE PACKAGES ───────────────────────────────────────────────────────────
###############################################################################
echo "· Updating APT cache and installing base packages …"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive \
  apt-get install -y --no-install-recommends \
    tzdata curl net-tools iproute2 iputils-ping \
    build-essential gcc g++ clang git make cmake \
    python3.11 python3.11-venv

###############################################################################
# 3 ▸ STATIC NODE REGISTRY (replaces Tailscale) ───────────────────────────────
###############################################################################
NODE_NAME="${NEBULA_NODE_NAME:-$(hostname)}"
echo "· Node name: ${NODE_NAME}"
if [[ -n "${NEBULA_NODES:-}" ]]; then
  echo "· Node registry: NEBULA_NODES (inline JSON/CSV)"
elif [[ -n "${NEBULA_NODE_REGISTRY_FILE:-}" ]]; then
  echo "· Node registry file: ${NEBULA_NODE_REGISTRY_FILE}"
else
  echo "⚠ NEBULA_NODES/NEBULA_NODE_REGISTRY_FILE no definidos."
  echo "  Este nodo no podrá validar vecinos hasta configurar el registro."
fi

###############################################################################
# 4 ▸ PYTHON 3.11 AS DEFAULT ──────────────────────────────────────────────────
###############################################################################
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 2
sudo update-alternatives --install /usr/bin/python  python  /usr/bin/python3     1

###############################################################################
# 5 ▸ CLONE / UPDATE REPOSITORY ───────────────────────────────────────────────
###############################################################################
if [[ ! -d .git ]]; then
  echo "· Cloning ${REPO_URL} (${REPO_BRANCH}) …"
  git clone --branch "${REPO_BRANCH}" "${REPO_URL}" .
else
  echo "· Updating repository …"
  git fetch origin "${REPO_BRANCH}"
  git checkout "${REPO_BRANCH}"
  git pull --ff-only
fi

###############################################################################
# 6 ▸ PERSIST NODE CONFIG (.env, ignorado por git) ───────────────────────────
###############################################################################
touch .env
grep -q '^NEBULA_NODE_NAME=' .env 2>/dev/null || echo "NEBULA_NODE_NAME=${NODE_NAME}" >> .env
if [[ -n "${NEBULA_NODES:-}" ]]; then
  grep -q '^NEBULA_NODES=' .env 2>/dev/null || echo "NEBULA_NODES=${NEBULA_NODES}" >> .env
fi
if [[ -n "${NEBULA_NODE_REGISTRY_FILE:-}" ]]; then
  grep -q '^NEBULA_NODE_REGISTRY_FILE=' .env 2>/dev/null || echo "NEBULA_NODE_REGISTRY_FILE=${NEBULA_NODE_REGISTRY_FILE}" >> .env
fi
if [[ -n "${NEBULA_NODE_IP:-}" ]]; then
  grep -q '^NEBULA_NODE_IP=' .env 2>/dev/null || echo "NEBULA_NODE_IP=${NEBULA_NODE_IP}" >> .env
fi

###############################################################################
# 7 ▸ UV & VENV ───────────────────────────────────────────────────────────────
###############################################################################
if ! command -v uv &>/dev/null; then
  echo "· Installing UV package manager …"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "· Creating Python ${PY_VERSION} virtual-env with uv …"
  uv python install "${PY_VERSION}"
  uv python pin     "${PY_VERSION}"
  uv venv "${VENV_DIR}"
fi

# Activate venv
# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

# Install project core dependencies (from pyproject.toml’s [tool.uv] groups)
uv sync --group core

###############################################################################
# 8 ▸ RUNTIME DEPS (Flask + Gunicorn) ─────────────────────────────────────────
###############################################################################
uv pip install --upgrade --no-cache-dir \
  "Flask>=3.0,<4.0" "gunicorn>=22.0"
