#!/bin/sh
cd "$(dirname "$0")" || exit 1
if ! command -v uv >/dev/null 2>&1; then
    echo "Wireless Flasher needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
exec uv run --quiet wireless_flasher.py "$@"
