#!/bin/bash
# Run the project's local Dial/tunnel tools with a supported Node runtime.
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  dial) entry="$root/.tools/node_modules/@getdial/cli/dist/cli.js" ;;
  cloudflared) entry="$root/.tools/node_modules/cloudflared/lib/cloudflared.js" ;;
  *) echo 'Usage: bash tools.sh dial|cloudflared [arguments...]' >&2; exit 2 ;;
esac
shift
if [[ ! -f "$entry" ]]; then
  echo 'Local tools are missing; follow the installation instructions in README.md.' >&2
  exit 1
fi
for runtime in "${TIMO_NODE:-node}" "$HOME/.nvm/versions/node/v24.19.0/bin/node" /opt/homebrew/bin/node; do
  if "$runtime" -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 19) ? 0 : 1)' >/dev/null 2>&1; then
    export DIAL_NO_AUTO_UPDATE=1
    exec "$runtime" "$entry" "$@"
  fi
done
echo 'Dial needs Node 22.19+; set TIMO_NODE to a compatible node executable.' >&2
exit 1
