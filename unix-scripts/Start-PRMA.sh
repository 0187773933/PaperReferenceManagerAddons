#!/usr/bin/env bash
# Get started with prma. If it isn't installed yet, run the installer first,
# then show the help.

if ! command -v prma >/dev/null 2>&1; then
    echo "prma not found. Running installer..."
    bash "$(dirname "$0")/Unix-Install.sh"
    # pipx ensurepath / pyenv shims only affect *new* shells, so prma may still
    # be missing in this one. Re-check before continuing.
    if ! command -v prma >/dev/null 2>&1; then
        echo
        echo "Install finished, but prma isn't on PATH in this shell yet."
        echo "Restart your shell (or open a new terminal), then run Start-PRMA.sh again."
        exit 0
    fi
fi

# prma resolves --config (and other paths) relative to the current directory,
# so run from the project root, not from unix-scripts/.
cd "$(dirname "$0")/.."

prma --help
