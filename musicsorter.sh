#!/usr/bin/env bash
# Runs MusicSorter from this folder, creating its private virtualenv on first use.
#   ./musicsorter.sh plan /path/to/Music --dest /path/to/Music
# To call it as `musicsorter` from anywhere:
#   ln -s "$PWD/musicsorter.sh" ~/.local/bin/musicsorter
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
venv="$here/.venv"
if ! "$venv/bin/python" -c "import mutagen, musicsorter" >/dev/null 2>&1; then
    echo "Setting up $venv ..." >&2
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --disable-pip-version-check -e "$here"
fi
exec "$venv/bin/python" -m musicsorter "$@"
