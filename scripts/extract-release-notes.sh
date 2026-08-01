#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <tag> <changelog> <output>" >&2
  exit 2
fi

tag="$1"
changelog="$2"
output="$3"

if [ ! -f "$changelog" ]; then
  echo "changelog does not exist: $changelog" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"

if ! awk -v heading="## ${tag}" '
  $0 == heading { found = 1; next }
  found && /^## / { exit }
  found { print }
  END { if (!found) exit 3 }
' "$changelog" > "$output"; then
  rm -f "$output"
  echo "release notes section is missing: ## ${tag}" >&2
  exit 1
fi

if ! grep -q '[^[:space:]]' "$output"; then
  rm -f "$output"
  echo "release notes section is empty: ## ${tag}" >&2
  exit 1
fi
