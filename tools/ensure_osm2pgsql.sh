#!/usr/bin/env bash
# Ensure a sufficiently new osm2pgsql is available for OSM Carto 6 flex Lua.
# Ubuntu 22.04 ships 1.6.0, which lacks object:as_point().
# Never replaces the OS binary; installs a pinned native release under app cache.
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo 'usage: ensure_osm2pgsql.sh <native-runtime-cache-root>' >&2
  exit 2
fi
BASE="$1"
MIN_VERSION=1.8.0
BUILD_VERSION=1.11.0
PREFIX="$BASE/native/osm2pgsql-$BUILD_VERSION"
LOCAL_BIN="$PREFIX/bin/osm2pgsql"

# Output contract: ONLY the selected absolute executable path on stdout.
# All progress/error messages go to stderr.
info() { printf '%s\n' "$*" >&2; }
version_of() {
  local output version
  output="$("$1" --version 2>&1)" || return 1
  version="$(printf '%s\n' "$output" | grep -Eom1 '[0-9]+\.[0-9]+(\.[0-9]+)?' || true)"
  [[ -n "$version" ]] || return 1
  printf '%s\n' "$version"
}
compatible() {
  local v
  v="$(version_of "$1")" || return 1
  dpkg --compare-versions "$v" ge "$MIN_VERSION"
}

# Prefer an already-built local binary over old system packages.
if [[ -x "$LOCAL_BIN" ]] && compatible "$LOCAL_BIN"; then
  info "[CHECK] Using cached osm2pgsql $(version_of "$LOCAL_BIN")"
  printf '%s\n' "$LOCAL_BIN"
  exit 0
fi

SYSTEM_BIN="$(command -v osm2pgsql || true)"
if [[ -n "$SYSTEM_BIN" ]] && compatible "$SYSTEM_BIN"; then
  info "[CHECK] Using installed osm2pgsql $(version_of "$SYSTEM_BIN")"
  printf '%s\n' "$SYSTEM_BIN"
  exit 0
fi

OLD_VERSION='not installed'
if [[ -n "$SYSTEM_BIN" ]]; then
  OLD_VERSION="$(version_of "$SYSTEM_BIN" || echo unknown)"
fi
info "[FIX] osm2pgsql $OLD_VERSION is incompatible with OSM Carto flex (need >=$MIN_VERSION)."
info "[FIX] Building osm2pgsql $BUILD_VERSION once in the application's WSL cache."

if [[ "$(id -u)" == 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null 2>&1 || { info '[ERROR] sudo required for first-time build dependency installation.'; exit 3; }
  SUDO=(sudo)
fi

# Source release bundles the appropriate libosmium, protozero and fmt headers.
# Build tools are needed only for the initial compilation, not for later runs.
"${SUDO[@]}" apt-get update 1>&2
"${SUDO[@]}" apt-get install -y --no-install-recommends \
  build-essential cmake pkg-config git \
  libboost-dev libboost-system-dev libboost-filesystem-dev libboost-thread-dev libexpat1-dev zlib1g-dev libbz2-dev \
  libpq-dev libproj-dev liblua5.3-dev lua5.3 nlohmann-json3-dev 1>&2

BUILD_ROOT="$BASE/build-osm2pgsql-$BUILD_VERSION"
SOURCE="$BUILD_ROOT/source"
BUILD="$BUILD_ROOT/build"
mkdir -p "$BUILD_ROOT"
if [[ ! -d "$SOURCE/.git" ]]; then
  rm -rf "$SOURCE"
  git clone --quiet --depth 1 --branch "$BUILD_VERSION" \
    https://github.com/osm2pgsql-dev/osm2pgsql.git "$SOURCE" 1>&2
fi
# Keep the source version fixed even if the workspace was reused.
[[ "$(git -C "$SOURCE" describe --tags --exact-match 2>/dev/null)" == "$BUILD_VERSION" ]] || {
  info '[ERROR] Unrecognized osm2pgsql source checkout; refusing to compile.'
  exit 4
}
cmake -S "$SOURCE" -B "$BUILD" \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF 1>&2

# Cap jobs to avoid overcommitting WSL memory while compiling Boost-heavy code.
JOBS=2
if [[ "$(nproc)" -lt 2 ]]; then JOBS=1; fi
cmake --build "$BUILD" --parallel "$JOBS" 1>&2
cmake --install "$BUILD" 1>&2
if [[ ! -x "$LOCAL_BIN" ]] || ! compatible "$LOCAL_BIN"; then
  info '[ERROR] Compiled osm2pgsql is missing or incompatible.'
  exit 4
fi
info "[FIX] Ready: $LOCAL_BIN ($(version_of "$LOCAL_BIN"))"
printf '%s\n' "$LOCAL_BIN"