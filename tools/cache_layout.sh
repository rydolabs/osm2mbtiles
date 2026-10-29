#!/usr/bin/env bash
# Shared WSL cache location and one-time migration from the former name.
# Source from bash scripts; does not create the new directory unless migrating.
OSM_CACHE_BASE="$HOME/.cache/osm2mbtiles"
OSM_CACHE_LEGACY="$HOME/.cache/osm2mbtiles-oneclick"

osm_cache_select() {
  if [[ -L "$OSM_CACHE_BASE" ]]; then
    echo "[ERROR] Refusing symlinked cache directory: $OSM_CACHE_BASE" >&2
    return 4
  fi
  if [[ -d "$OSM_CACHE_LEGACY" && ! -e "$OSM_CACHE_BASE" ]]; then
    if [[ -L "$OSM_CACHE_LEGACY" ]]; then
      echo "[ERROR] Refusing symlinked legacy cache: $OSM_CACHE_LEGACY" >&2
      return 4
    fi
    # Prevent moving the cache while an older launcher is using it.
    (
      exec 9>"$OSM_CACHE_LEGACY/.conversion.lock"
      if command -v flock >/dev/null 2>&1; then
        flock -n 9 || {
          echo '[ERROR] Existing conversion is running; cache migration refused.' >&2
          exit 5
        }
      fi
      local pidfile="$OSM_CACHE_LEGACY/work/pgdata/postmaster.pid"
      if [[ -f "$pidfile" ]]; then
        local pgpid=''
        read -r pgpid < "$pidfile" || true
        if [[ "$pgpid" =~ ^[0-9]+$ ]] && kill -0 "$pgpid" 2>/dev/null; then
          echo '[ERROR] PostgreSQL is still active; cache migration refused.' >&2
          exit 5
        fi
      fi
      mv -- "$OSM_CACHE_LEGACY" "$OSM_CACHE_BASE"
      echo "[CACHE] Migrated existing WSL cache: $OSM_CACHE_LEGACY -> $OSM_CACHE_BASE" >&2
    ) || return $?
  elif [[ -d "$OSM_CACHE_LEGACY" && -d "$OSM_CACHE_BASE" ]]; then
    echo '[WARN] Both old and new cache directories exist; using ~/.cache/osm2mbtiles.' >&2
    echo "[WARN] Legacy data was not deleted or merged: $OSM_CACHE_LEGACY" >&2
  fi
}