#!/usr/bin/env bash
# Invoked by run_windows.ps1 inside Ubuntu/Debian WSL.
# No Docker and no system PostgreSQL service configuration by the user.
set -Eeuo pipefail

if [[ $# -lt 7 || $# -gt 13 ]]; then
  echo 'usage: run_wsl.sh <project-win> <pbf-win> <out-win> <zoom> <workers> <cleanup> <format> [vector-sql-mode] [vector-import-profile] [vector-tiler] [batch-side] [raster-format] [bbox]' >&2
  exit 2
fi
PROJECT="$(wslpath -a "$1")"
# Sentinel means automatic Geofabrik download into the Linux-native WSL
# filesystem. This avoids slower PostgreSQL reads via /mnt/c.
AUTO_DOWNLOAD=0
if [[ "$2" == '--geofabrik-south-korea' ]]; then
  AUTO_DOWNLOAD=1
  PBF=''
else
  PBF="$(wslpath -a "$2")"
fi
OUTPUT="$(wslpath -a "$3")"
ZOOM="$4"
WORKERS="$5"
CLEANUP="$6"
FORMAT="$7"
QUERY_MODE="${8:-projected}"
IMPORT_PROFILE="${9:-safe}"
VECTOR_TILER="${10:-postgis}"
BATCH_SIDE="${11:-2}"
RASTER_FORMAT="${12:-png}"
BBOX="${13:-}"
if [[ ! -f "$PROJECT/pyproject.toml" ]] || { [[ "$AUTO_DOWNLOAD" != 1 ]] && [[ ! -f "$PBF" ]]; }; then
  echo '[ERROR] WSL cannot locate PBF or project files.' >&2
  exit 2
fi
if [[ ! "$ZOOM" =~ ^[0-9]+(-[0-9]+)?$ || ! "$WORKERS" =~ ^[0-9]+$ ]]; then
  echo '[ERROR] Invalid zoom or process count.' >&2
  exit 2
fi

case "$QUERY_MODE" in projected|baseline|cached) ;; *) echo '[ERROR] Vector SQL mode must be projected/baseline/cached' >&2; exit 2 ;; esac
case "$IMPORT_PROFILE" in safe|fast) ;; *) echo '[ERROR] Vector import profile must be safe/fast' >&2; exit 2 ;; esac
case "$VECTOR_TILER" in postgis|batch) ;; *) echo '[ERROR] Vector tiler must be postgis/batch' >&2; exit 2 ;; esac
case "$BATCH_SIDE" in [1-8]) ;; *) echo '[ERROR] Batch side must be 1..8' >&2; exit 2 ;; esac
case "$RASTER_FORMAT" in png|png256) ;; *) echo '[ERROR] Raster encoding must be png/png256' >&2; exit 2 ;; esac
case "$FORMAT" in raster|vector|both) ;; *) echo '[ERROR] Format must be raster/vector/both' >&2; exit 2 ;; esac
case "$CLEANUP" in keep|compact|prune) ;; *) echo '[ERROR] Unknown cache policy (keep/compact/prune).' >&2; exit 2 ;; esac

# All DB/cache/temp writes stay inside WSL's native ext4 filesystem, not /mnt/c.
source "$PROJECT/tools/cache_layout.sh"
osm_cache_select
BASE="$OSM_CACHE_BASE"
RUNTIME="$BASE/runtime"
STYLE="$BASE/style-v6.0.0"
WORKDIR="$BASE/work"
EXPORTDIR="$BASE/exports"
mkdir -p "$BASE" "$EXPORTDIR"
# Preserve failure evidence on the WSL filesystem so that a PostgreSQL crash
# can be diagnosed even if pg_ctl can no longer stop the dead postmaster.
print_failure_diagnostics() {
  local status="$1" line="$2" failed_command="$3"
  trap - ERR
  echo "[DIAG] Failed command at run_wsl.sh:$line: $failed_command" >&2
  echo "[DIAG] Actual input PBF: ${PBF:-<not-yet-selected>}" >&2
  echo "[DIAG] Tile bbox: ${BBOX:-<PBF header>}" >&2
  echo "[DIAG] WSL pipeline stopped (exit=$status)" >&2
  if [[ -f "$WORKDIR/postgres.log" ]]; then
    echo "[DIAG] PostgreSQL logfile: $WORKDIR/postgres.log" >&2
    tail -n 70 "$WORKDIR/postgres.log" >&2 || true
  fi
  echo '[DIAG] Linux memory:' >&2
  free -h >&2 || true
  echo '[DIAG] WSL work volume:' >&2
  df -h "$BASE" >&2 || true
  if [[ -e /sys/fs/cgroup/memory.events ]]; then
    echo '[DIAG] cgroup memory.events (oom/oom_kill counters):' >&2
    cat /sys/fs/cgroup/memory.events >&2 || true
  fi
  # Reading the kernel log may be restricted to root on many WSL installs.
  if command -v dmesg >/dev/null 2>&1; then
    dmesg 2>/dev/null | grep -iE 'out of memory|oom|killed process|segfault' | tail -n 16 >&2 || true
  fi
  echo "[DIAG] Safe to rerun. Source PBF is unchanged: ${PBF:-<unknown>}" >&2
  exit "$status"
}
trap 'print_failure_diagnostics $? "$LINENO" "$BASH_COMMAND"' ERR
# Preflight is informational: the user's disk image location and input region
# may vary, so do not invent a universal minimum free-space threshold.
echo '[CHECK] WSL memory and available disk for PostgreSQL import:'
free -h || true
df -h "$BASE" || true
# A shared, reusable PG workdir must never be imported or rendered by two
# processes at once. Block concurrent launcher executions with a clear error.
exec 8>"$BASE/.conversion.lock"
if command -v flock >/dev/null 2>&1; then
  flock -n 8 || { echo '[ERROR] Another osm2mbtiles conversion is already running.' >&2; exit 5; }
fi
if [[ "$AUTO_DOWNLOAD" == 1 ]]; then
  PBF="$BASE/downloads/south-korea-latest.osm.pbf"
fi
export DEBIAN_FRONTEND=noninteractive

# Avoid system PostgreSQL configuration; installed binaries only are used.
need_packages=0
for tool in git curl osm2pgsql ogr2ogr npm fc-cache; do
  if ! command -v "$tool" >/dev/null 2>&1; then need_packages=1; fi
done
if ! /usr/bin/python3 -c 'import mapnik, requests, psycopg2, yaml, PIL' >/dev/null 2>&1; then
  need_packages=1
fi
if [[ -z "$(find /usr/lib/postgresql -maxdepth 3 -name initdb -print -quit 2>/dev/null)" ]]; then
  need_packages=1
fi
if ! dpkg-query -W -f='${Status}' fonts-hanazono 2>/dev/null | grep -q 'install ok installed'; then
  need_packages=1
fi
if ! dpkg-query -W -f='${Status}' 'postgresql-*-postgis-3' 2>/dev/null | grep -q 'install ok installed'; then
  need_packages=1
fi
if [[ "$need_packages" == 1 ]]; then
  echo '[SETUP] Installing native Mapnik, PostgreSQL/PostGIS and Carto dependencies in WSL...'
  if [[ "$(id -u)" == 0 ]]; then SUDO=(); else
    command -v sudo >/dev/null 2>&1 || { echo '[ERROR] WSL needs sudo for first-time native package install' >&2; exit 3; }
    SUDO=(sudo)
  fi
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y --no-install-recommends \
    postgresql postgresql-client postgresql-contrib osm2pgsql gdal-bin \
    python3-mapnik python3-psycopg2 python3-requests python3-yaml python3-pil \
    fonts-hanazono git curl npm fontconfig ca-certificates
  major="$(find /usr/lib/postgresql -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)"
  if [[ -z "$major" ]]; then echo '[ERROR] PostgreSQL installation incomplete' >&2; exit 3; fi
  "${SUDO[@]}" apt-get install -y --no-install-recommends \
    "postgresql-$major-postgis-3" "postgresql-$major-postgis-3-scripts"
fi
# OSM Carto 6 flex requires object:as_point() (osm2pgsql >=1.7, and
# upstream documents >=1.8). Ubuntu 22.04 ships 1.6.0. Detect this BEFORE
# importing the entire Korean PBF; build a private 1.11.0 binary if required.
# Cache and OS packages remain intact. Capture stdout: only the executable path.
OSM2PGSQL_BIN="$(bash "$PROJECT/tools/ensure_osm2pgsql.sh" "$BASE")"
if [[ ! -x "$OSM2PGSQL_BIN" ]]; then
  echo '[ERROR] Failed to select a compatible osm2pgsql binary.' >&2
  exit 3
fi
export PATH="$(dirname "$OSM2PGSQL_BIN"):$PATH"
echo "[CHECK] osm2pgsql binary: $OSM2PGSQL_BIN"
"$OSM2PGSQL_BIN" --version

if [[ "$AUTO_DOWNLOAD" == 1 ]]; then
  # Reruns keep a complete cached PBF and conditionally refresh it using
  # the remote Last-Modified date; incomplete files retain a .part suffix.
  bash "$PROJECT/tools/download_geofabrik.sh" "$PBF"
fi
if ! command -v carto >/dev/null 2>&1; then
  echo '[SETUP] Installing CartoCSS compiler into WSL...'
  if [[ "$(id -u)" == 0 ]]; then npm install --global carto@1.2.0; else sudo npm install --global carto@1.2.0; fi
fi

# Keep a pinned stylesheet. Never silently change versions across executions.
if [[ ! -f "$STYLE/project.xml" || ! -f "$STYLE/.osm2mbtiles-fonts-ready" ]]; then
  echo '[SETUP] Preparing OpenStreetMap Carto v6.0.0 and fonts (network required)...'
  if [[ ! -d "$STYLE/.git" ]]; then
    rm -rf "$STYLE"
    git clone --depth 1 --branch v6.0.0 \
      https://github.com/openstreetmap-carto/openstreetmap-carto.git "$STYLE"
  fi
  (
    cd "$STYLE"
    carto -a 3.0.22 project.mml > project.xml
    # The Hanazono OSDN mirror is discontinued. Ubuntu's fonts-hanazono
    # package supplies the matching HanaMinA/B faces; other upstream files
    # are fetched from their pinned URLs.
    /usr/bin/python3 - <<'PY'
from pathlib import Path
p = Path('scripts/get-fonts.py')
s = p.read_text(encoding='utf-8')
marker = 'downloadToFile(\n    ["https://mirrors.dotsrc.org/osdn/hanazono-font/'
if marker in s:
    start = s.index(marker)
    end = s.index('# clean up tmp directories', start)
    p.write_text(s[:start] + '# Hanazono fonts supplied via Ubuntu package\n' + s[end:], encoding='utf-8')
PY
    mkdir -p fonts
    /usr/bin/python3 scripts/get-fonts.py
    for font in HanaMinA HanaMinB; do
      candidate="/usr/share/fonts/truetype/hanazono/$font.ttf"
      [[ -f "$candidate" ]] || { echo "[ERROR] Missing $candidate" >&2; exit 3; }
      ln -sf "$candidate" "fonts/$font.ttf"
    done
    touch .osm2mbtiles-fonts-ready
  )
fi

# stage_from_system.py symlinks installed tools and Python with Mapnik.
if [[ ! -f "$RUNTIME/styles/osm-carto/project.xml" ]]; then
  echo '[SETUP] Staging isolated application runtime...'
  PATH="$(dirname "$(find /usr/lib/postgresql -maxdepth 3 -name initdb -print -quit)"):$PATH" \
  /usr/bin/python3 "$PROJECT/tools/stage_from_system.py" \
    --style-src "$STYLE" --dest "$RUNTIME" --mapnik-python /usr/bin/python3
fi
export PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}"

# The staged runtime may have cached a symlink to the original 1.6.0
# executable. Refresh just that symlink (do NOT copy/remove cached style data).
mkdir -p "$RUNTIME/bin"
ln -sfn -- "$OSM2PGSQL_BIN" "$RUNTIME/bin/osm2pgsql"
"$RUNTIME/bin/osm2pgsql" --version

# Preflight tile count before importing the PBF or downloading the large assets.
EXPORT_NAME="$(basename "$OUTPUT")"
LOCAL_OUT="$EXPORTDIR/$EXPORT_NAME"
BBOX_ARGS=()
if [[ -n "$BBOX" ]]; then
  BBOX_ARGS=(--bbox "$BBOX")
  echo "[CHECK] Explicit tile bbox: $BBOX"
fi
/usr/bin/python3 -m osm2mbtiles "$PBF" -z "$ZOOM" -o "$LOCAL_OUT" "${BBOX_ARGS[@]}" --dry-run
/usr/bin/python3 -m osm2mbtiles doctor --runtime "$RUNTIME"
echo "[RUN] Converting PBF to $FORMAT MBTiles."
RASTER_ARGS=()
if [[ "$FORMAT" != vector ]]; then
  RASTER_ARGS=(--raster-format "$RASTER_FORMAT")
fi
/usr/bin/python3 -m osm2mbtiles "$PBF" -z "$ZOOM" -o "$LOCAL_OUT" \
  --runtime "$RUNTIME" --workdir "$WORKDIR" --processes "$WORKERS" \
  --metatile 8 --tile-type "$FORMAT" "${RASTER_ARGS[@]}" "${BBOX_ARGS[@]}" \
  --vector-tiler "$VECTOR_TILER" --vector-batch-side "$BATCH_SIDE" \
  --vector-query-mode "$QUERY_MODE" --vector-import-profile "$IMPORT_PROFILE" --force

# For both, CLI adds raster/vector suffixes. Never copy an unverified file.
BASE_OUT="${OUTPUT%.mbtiles}"
BASE_LOCAL="${LOCAL_OUT%.mbtiles}"
case "$FORMAT" in
  raster) FILES=("$LOCAL_OUT:$OUTPUT:raster") ;;
  vector) FILES=("$LOCAL_OUT:$OUTPUT:vector") ;;
  both) FILES=("$BASE_LOCAL.raster.mbtiles:$BASE_OUT.raster.mbtiles:raster"
              "$BASE_LOCAL.vector.mbtiles:$BASE_OUT.vector.mbtiles:vector") ;;
esac
for item in "${FILES[@]}"; do
  IFS=: read -r SRC DEST FMT <<< "$item"
  if [[ "$FMT" == vector ]]; then
    /usr/bin/python3 -m osm2mbtiles verify-vector "$SRC"
  else
    /usr/bin/python3 -m osm2mbtiles verify "$SRC"
  fi
  mkdir -p "$(dirname "$DEST")"
  cp -- "$SRC" "$DEST.tmp"
  if ! cmp -s -- "$SRC" "$DEST.tmp"; then
    echo '[ERROR] Windows copy differs from generated MBTiles; WSL original preserved.' >&2
    rm -f -- "$DEST.tmp"
    exit 6
  fi
  mv -f -- "$DEST.tmp" "$DEST"
  echo "[SUCCESS] Byte-verified $FMT MBTiles: $DEST"
done
# Remove WSL exports only after ALL outputs safely copied to Windows.
for item in "${FILES[@]}"; do
  IFS=: read -r SRC DEST FMT <<< "$item"
  name="$(basename "$SRC")"
  # prune should run once after all duplicate exports were removed.
  mode=compact
  if [[ "$FMT" == "${FORMAT}" || "$item" == "${FILES[-1]}" ]]; then mode="$CLEANUP"; fi
  if ! bash "$PROJECT/tools/cache_cleanup.sh" "$mode" "$name" --runner-locked; then
    echo '[WARN] Output complete but optional WSL cache cleanup failed' >&2
  fi
done