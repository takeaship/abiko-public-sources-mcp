#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor
clone() {
  local repo="$1" tag="$2" sha="$3" dir="$4"
  if [[ ! -d "$dir/.git" ]]; then git clone --depth 1 --branch "$tag" "$repo" "$dir"; fi
  [[ "$(git -C "$dir" rev-parse HEAD)" == "$sha" ]] || { echo "Wrong revision: $dir" >&2; exit 1; }
}
clone https://github.com/unclecode/crawl4ai.git v0.9.5 838869bdda93f25e6d50b3f50ae9577d9b19a586 vendor/crawl4ai
if [[ ! -f .env ]]; then
  umask 077
  printf 'CRAWL4AI_API_TOKEN=%s\n' "$(openssl rand -hex 32)" > .env
fi
