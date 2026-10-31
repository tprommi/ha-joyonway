#!/usr/bin/env bash

set -euo pipefail

archive="/tmp/joyonway-develop.tar.gz"
target="/tmp/joyonway-develop"

if [[ ! -f "$archive" ]]; then
    printf 'Fehler: Archiv nicht gefunden: %s\n' "$archive" >&2
    exit 1
fi

mkdir -p "$target"
tar -xzf "$archive" -C "$target"
printf 'Entpackt nach %s/custom_components/joyonway\n' "$target"
