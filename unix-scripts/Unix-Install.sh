#!/usr/bin/env bash
set -euo pipefail

# Install prma as an isolated CLI via pipx, on a pyenv-managed Python.
# Bootstraps pyenv if missing and installs the version pinned in .python-version.
# Lives in unix-scripts/; cd's up to the project root so it works from anywhere.

cd "$(dirname "$0")/.."

EDITABLE="-e"   # drop the -e for a non-editable install

# --- pyenv ---
if ! command -v pyenv >/dev/null 2>&1; then
    echo "pyenv not found, installing..."
    curl -fsSL https://pyenv.run | bash
    echo "Add these to your shell rc (~/.zshrc or ~/.bashrc) for future shells:"
    echo '  export PYENV_ROOT="$HOME/.pyenv"'
    echo '  export PATH="$PYENV_ROOT/bin:$PATH"'
    echo '  eval "$(pyenv init -)"'
fi
export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"

# --- version from .python-version ---
if [ ! -f .python-version ]; then
    echo "ERROR: no .python-version in $(pwd). Run: pyenv local <version>"
    exit 1
fi
PYVER="$(tr -d '[:space:]' < .python-version)"
echo "Target Python: $PYVER"
pyenv install -s "$PYVER"     # -s = skip if already built

PYTHON="$(pyenv which python)"

# --- pipx ---
if ! command -v pipx >/dev/null 2>&1; then
    echo "pipx not found, installing..."
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath
    echo "PATH updated. Restart your shell, then re-run this script."
    exit 0
fi

# Fresh install so --python actually takes effect.
pipx uninstall prma >/dev/null 2>&1 || true
pipx install --python "$PYTHON" $EDITABLE .

# Runtime deps via real pip (honors flags like --only-binary in requirements.txt).
pipx runpip prma install -r requirements.txt

# Pin numpy below 2 last so it wins.
pipx inject prma 'numpy<2'

echo "Done. Installed with: $PYTHON"
echo "Try: prma --help"
echo "(If 'prma' isn't found, restart your shell so the new PATH takes effect.)"