#!/usr/bin/env bash
# run.sh — Launch OpenEvolve hyperparameter search for OpenZL training.
#
# Usage:
#   bash nyx/evolve/run.sh                    # default 50 iterations
#   bash nyx/evolve/run.sh --iterations 100   # custom iteration count
#
# Prerequisites:
#   1. nyx venv active with openevolve installed
#   2. nyx built ('nyx build')
#   3. Data prepared ('bash nyx/evolve/prepare_data.sh')
#   4. API key saved to api_key.txt in repo root
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
NYX_ROOT="${REPO_ROOT}/nyx"

INITIAL="${SCRIPT_DIR}/initial_program.py"
EVALUATOR="${SCRIPT_DIR}/evaluator.py"
CONFIG="${SCRIPT_DIR}/config.yaml"
API_KEY_FILE="${REPO_ROOT}/api_key.txt"
OUTPUT_DIR="${SCRIPT_DIR}/openevolve_output"
NYX_VENV="${NYX_ROOT}/.venv"

# ── Activate the nyx venv (Python 3.10+ required by OpenEvolve) ──────────────
if [ -d "${NYX_VENV}" ]; then
    source "${NYX_VENV}/bin/activate"
else
    echo "ERROR: nyx venv not found at ${NYX_VENV}"
    echo "  Create it with: python3.13 -m venv ${NYX_VENV}"
    echo "  Then install nyx + openevolve:"
    echo "    pip install -e ${NYX_ROOT}"
    echo "    pip install -r ${SCRIPT_DIR}/requirements.txt"
    exit 1
fi

# ── Load API key ─────────────────────────────────────────────────────────────
if [ -f "${API_KEY_FILE}" ]; then
    export OPENAI_API_KEY="$(cat "${API_KEY_FILE}" | tr -d '[:space:]')"
    echo "Loaded API key from ${API_KEY_FILE}"
else
    if [ -z "${OPENAI_API_KEY:-}" ]; then
        echo "ERROR: No API key found."
        echo "  Either create ${API_KEY_FILE} with your OpenAI key,"
        echo "  or set the OPENAI_API_KEY environment variable."
        exit 1
    fi
    echo "Using OPENAI_API_KEY from environment"
fi

# ── Preflight checks ────────────────────────────────────────────────────────
echo "=== OpenEvolve: OpenZL Hyperparameter Search ==="

# Check that nyx is built
ZLI="${NYX_ROOT}/openzl/zli"
if [ ! -x "${ZLI}" ]; then
    echo "ERROR: zli not found at ${ZLI}. Run 'nyx build' first."
    exit 1
fi

# Check that data is prepared
EVOLVE_DATA="${REPO_ROOT}/evolve_data"
if [ ! -d "${EVOLVE_DATA}/test_chunks" ]; then
    echo "ERROR: Evaluation data not found."
    echo "  Run: bash nyx/evolve/prepare_data.sh"
    exit 1
fi

# Check openevolve is installed
if ! python3 -c "import openevolve" 2>/dev/null; then
    echo "ERROR: openevolve not installed."
    echo "  Run: pip install -r nyx/evolve/requirements.txt"
    exit 1
fi

# ── Launch evolution ─────────────────────────────────────────────────────────
echo ""
echo "Initial program: ${INITIAL}"
echo "Evaluator:       ${EVALUATOR}"
echo "Config:          ${CONFIG}"
echo "Output:          ${OUTPUT_DIR}"
echo ""

# Pass through any extra arguments (e.g. --iterations 100)
exec python3 -m openevolve.cli \
    "${INITIAL}" \
    "${EVALUATOR}" \
    --config "${CONFIG}" \
    --output "${OUTPUT_DIR}" \
    "$@"
