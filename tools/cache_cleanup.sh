#!/usr/bin/env bash
# Called ONLY after a completed and verified Windows MBTiles copy.
# Standalone invocation: bash cache_cleanup.sh status|compact|prune|keep
set -Eeuo pipefail
MODE="${1:-status}"
# This script may be invoked directly by cleanup.bat; it must migrate the
# old cache name just as run_wsl.sh does.
source "$(dirname "${BASH_SOURCE[0]}")/cache_layout.sh"
osm_cache_select
BASE="$OSM_CACHE_BASE"
EXPORT_NAME="${2:-}"
RUNNER_LOCKED="${3:-}"
COPIED_TO="${4:-}"
case "$MODE" in
  status|keep|compact|prune) ;;
  *) echo "[ERROR] Unknown cache cleanup mode: $MODE (use status, keep, compact, prune)" >&2; exit 2 ;;
esac
if [[ ! -d "$BASE" ]]; then
  echo "[CACHE] Cache does not exist: $BASE"
  exit 0
fi
# Report sizes separately. The work directory includes the imported PostGIS DB.
report() {
  echo "[CACHE] WSL cache: $BASE"
  local item
  for item in downloads native runtime style-v6.0.0 work exports; do
    if [[ -e "$BASE/$item" ]]; then
      du -sh -- "$BASE/$item" 2>/dev/null || true
    fi
  done
}
if [[ "$MODE" == status ]]; then report; exit 0; fi
if [[ "$MODE" == keep && -z "$EXPORT_NAME" ]]; then
  echo '[CACHE] keep: retain downloaded PBF, imported DB, and native tool caches.'
  exit 0
fi
# Since the WSL launcher's flock is already acquired, do not lock twice.
# Manual cleanup requires the same lock and refuses to run during an export.
if [[ "$RUNNER_LOCKED" != --runner-locked ]]; then
  exec 7>"$BASE/.conversion.lock"
  if command -v flock >/dev/null 2>&1; then
    flock -n 7 || { echo '[CACHE] Conversion running; refusing cleanup.' >&2; exit 5; }
  fi
fi
if [[ -n "$EXPORT_NAME" ]]; then
  # The launcher passes the basename only; never permit directory traversal.
  if [[ "$EXPORT_NAME" != "$(basename -- "$EXPORT_NAME")" || "$EXPORT_NAME" != *.mbtiles ]]; then
    echo '[ERROR] Unsafe MBTiles export file name; cache untouched.' >&2
    exit 3
  fi
  # Only the conversion runner can assert that the Windows copy was already
  # byte-verified. Manual cleanup has to provide the existing Windows file
  # and prove that it is exactly the same before deleting the cache copy.
  if [[ "$RUNNER_LOCKED" == --runner-locked ]]; then
    # Called only after the launcher verified the full Windows output.
    rm -f -- "$BASE/exports/$EXPORT_NAME" \
             "$BASE/exports/$EXPORT_NAME.partial" \
             "$BASE/exports/$EXPORT_NAME.partial-wal" \
             "$BASE/exports/$EXPORT_NAME.partial-shm"
    echo "[CACHE] Removed duplicated WSL MBTiles after verified copy: exports/$EXPORT_NAME"
  elif [[ -f "$BASE/exports/$EXPORT_NAME" ]]; then
    if [[ -z "$COPIED_TO" || ! -f "$COPIED_TO" ]]; then
      echo '[CACHE] Cannot remove export without an existing verified copy.' >&2
      exit 6
    fi
    if ! cmp -s -- "$BASE/exports/$EXPORT_NAME" "$COPIED_TO"; then
      echo '[CACHE] Export differs from Windows output; preserving WSL copy.' >&2
      exit 6
    fi
    rm -f -- "$BASE/exports/$EXPORT_NAME" \
             "$BASE/exports/$EXPORT_NAME.partial" \
             "$BASE/exports/$EXPORT_NAME.partial-wal" \
             "$BASE/exports/$EXPORT_NAME.partial-shm"
    echo "[CACHE] Removed duplicated WSL MBTiles after verified copy: exports/$EXPORT_NAME"
  else
    # compact mode already deleted this file at completion. It must not
    # prevent a later, user-confirmed PostgreSQL DB prune.
    echo '[CACHE] WSL duplicate already absent; nothing to remove.'
  fi
else
  echo '[CACHE] No exported MBTiles specified; leaving exports untouched.'
fi
if [[ "$MODE" == prune ]]; then
  WORK="$BASE/work"
  PGDATA="$WORK/pgdata"
  if [[ -d "$PGDATA" ]]; then
    if [[ ! -f "$WORK/.osm2mbtiles-workspace" || ! -f "$PGDATA/PG_VERSION" || -L "$PGDATA" || -f "$PGDATA/postmaster.pid" ]]; then
      echo '[CACHE] Refusing DB pruning: missing ownership marker, running database, or unsafe pgdata.' >&2
      exit 4
    fi
    rm -rf -- "$PGDATA"
    echo '[CACHE] Deleted cached private PostgreSQL database.'
  fi
  # Never retain an import-state claiming PG is valid after removing pgdata.
  if [[ -f "$WORK/.osm2mbtiles-workspace" ]]; then
    rm -f -- "$WORK/import-state.json"
  fi
  echo '[CACHE] Kept PBF, styles/fonts, and native toolchain. The next run must reimport.'
fi
report