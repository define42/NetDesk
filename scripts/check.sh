#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_dir"

if ! command -v shellcheck >/dev/null 2>&1; then
    printf 'shellcheck is required; install it with your package manager.\n' >&2
    exit 1
fi

checked=0
while IFS= read -r -d '' file; do
    first_line=
    IFS= read -r first_line < "$file" || true
    case "$first_line" in
        '#!'*bash*)
            shell='bash'
            ;;
        '#!/bin/sh' | '#!/bin/sh '* | '#!/usr/bin/env sh' | '#!/usr/bin/env sh '* | '#!'*openrc-run*)
            shell='sh'
            ;;
        *)
            continue
            ;;
    esac

    "$shell" -n "$file"
    shellcheck --shell="$shell" "$file"
    checked=$((checked + 1))
done < <(find scripts overlay -type f -print0)

if ((checked == 0)); then
    printf 'No shell scripts found.\n' >&2
    exit 1
fi

printf 'Checked %d shell scripts.\n' "$checked"
