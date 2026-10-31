#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_dir="$(cd -- "$script_dir/.." && pwd)"
if git -C "$workspace_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    repo_dir="$workspace_dir"
else
    repo_dir="$(cd -- "$workspace_dir/develop" && pwd)"
fi
branch="$(git -C "$repo_dir" branch --show-current)"
archive_name="joyonway-develop.tar.gz"
remote_target="pi@nas:/tmp/$archive_name"

if [[ "$branch" != "develop" ]]; then
    printf 'Fehler: Dieses Skript erwartet den develop-Worktree (aktuell: %s).\n' "$branch" >&2
    exit 1
fi

if [[ ! -f "$repo_dir/custom_components/joyonway/manifest.json" ]]; then
    printf 'Fehler: custom_components/joyonway/manifest.json fehlt.\n' >&2
    exit 1
fi

archive_path="$(mktemp "/tmp/$archive_name.XXXXXX")"
trap 'rm -f "$archive_path"' EXIT

tar \
    --exclude='__pycache__' \
    --exclude='*.py[cod]' \
    -czf "$archive_path" \
    -C "$repo_dir" \
    custom_components/joyonway

if [[ "${1:-}" == "--dry-run" ]]; then
    printf 'Archivinhalt:\n'
    tar -tzf "$archive_path"
    printf '\nWürde kopieren nach: %s\n' "$remote_target"
    exit 0
fi

scp "$archive_path" "$remote_target"
printf 'Exportiert: %s\n' "$remote_target"
