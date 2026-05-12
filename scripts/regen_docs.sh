#!/bin/bash
# scripts/regen_docs.sh — Regenerate agent-system.md from template
# Replaces <!-- INCLUDE:path --> and <!-- INCLUDE_YAML_EXAMPLE --> markers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/agent-system.md.tmpl"
OUTPUT="${SCRIPT_DIR}/agent-system.md"
CADDY="/data/docs/agent-system.md"

lang() {
    case "$1" in
        *.sh)      echo bash ;;
        *.py)      echo python ;;
        *.gitignore) echo gitignore ;;
        *)         echo text ;;
    esac
}

while IFS= read -r line; do
    case "$line" in
        "<!-- INCLUDE_YAML_EXAMPLE -->")
            echo '```yaml'
            head -30 "${SCRIPT_DIR}/server/handover.yaml" 2>/dev/null || echo "# (not found)"
            echo '```'
            ;;
        "<!-- INCLUDE_ARCHITECTURE_TREE -->")
            echo '```'
            echo '/opt/projects/'
            echo '  agent.sh'
            echo '  agent-system.md.tmpl'
            echo '  agent-system.md'
            echo '  scripts/'
            for f in "${SCRIPT_DIR}/scripts"/*.sh; do
                echo "    $(basename "$f")"
            done
            echo '  server/'
            echo '    handover.yaml  handover.yaml.bak  handover_recent.yaml'
            echo '    CLAUDE.yaml  state.yaml  blueprint.yaml  changelog.yaml'
            echo '    .handover.lock  .last-structural-hash'
            echo '    archive/  logs/  scripts/'
            echo '```'
            ;;
        "<!-- INCLUDE:"*" -->")
            inc="${line#<!-- INCLUDE:}"; inc="${inc%% -->}"
            inc="${inc//[[:space:]]/}"
            if [ -f "$inc" ]; then
                echo '```'"$(lang "$inc")"
                cat "$inc"
                echo '```'
            else
                echo "<!-- MISSING: $inc -->"
            fi
            ;;
        *)
            printf '%s\n' "$line"
            ;;
    esac
done < "$TEMPLATE" > "$OUTPUT"

[ -w "$(dirname "$CADDY")" ] && cp "$OUTPUT" "$CADDY" 2>/dev/null || true
echo "[OK] agent-system.md regenerated"
