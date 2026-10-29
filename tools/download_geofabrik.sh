#!/usr/bin/env bash
# Download/update the Geofabrik South Korea PBF with a safe, reusable cache.
# Usage: download_geofabrik.sh /absolute/cache/south-korea-latest.osm.pbf [URL]
# The URL override is for offline integration testing only; the one-click
# launcher always uses the pinned Geofabrik URL below.
set -Eeuo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo 'usage: download_geofabrik.sh <destination.osm.pbf> [source-url]' >&2
  exit 2
fi
DEST="$1"
URL="${2:-https://download.geofabrik.de/asia/south-korea-latest.osm.pbf}"
if [[ "$DEST" != /* ]]; then
  echo '[ERROR] PBF cache destination must be an absolute Linux path.' >&2
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo '[ERROR] curl is required inside WSL.' >&2; exit 3; }
mkdir -p -- "$(dirname -- "$DEST")"

# Serialize downloader cache updates (the WSL launcher also locks conversion).
exec 9>"${DEST}.lock"
if command -v flock >/dev/null 2>&1; then flock 9; fi

RETRIES="${OSM2MBTILES_DOWNLOAD_RETRIES:-4}"
RETRY_DELAY="${OSM2MBTILES_DOWNLOAD_RETRY_DELAY:-3}"
CURL_COMMON=(
  --fail --location --progress-bar --retry "$RETRIES"
  --retry-delay "$RETRY_DELAY" --retry-all-errors --connect-timeout 30
)
PART="${DEST}.part"
REFRESH="${DEST}.refresh.part"
# Revalidation always writes to a fresh temporary file. An older, valid PBF
# is never replaced until the new download finishes successfully.
if [[ -s "$DEST" ]]; then
  rm -f -- "$REFRESH"
  echo '[DOWNLOAD] Checking Geofabrik for a newer South Korea PBF...'
  if HTTP_CODE="$(curl "${CURL_COMMON[@]}" \
      --time-cond "$DEST" --remote-time \
      --output "$REFRESH" --write-out '%{http_code}' "$URL")"; then
    case "$HTTP_CODE" in
      304)
        rm -f -- "$REFRESH"
        echo "[CACHE] Latest available copy is already cached: $DEST"
        ;;
      200|206)
        if [[ ! -s "$REFRESH" ]]; then
          echo '[ERROR] Server returned an empty PBF update.' >&2
          exit 4
        fi
        mv -f -- "$REFRESH" "$DEST"
        rm -f -- "$PART"
        echo "[DOWNLOAD] Updated cached PBF: $DEST"
        ;;
      *)
        rm -f -- "$REFRESH"
        echo "[ERROR] Unexpected HTTP status while checking PBF: $HTTP_CODE" >&2
        exit 4
        ;;
    esac
  else
    rm -f -- "$REFRESH"
    echo "[ERROR] Could not check Geofabrik for latest input. Existing PBF kept intact: $DEST" >&2
    exit 4
  fi
else
  # A previously interrupted download may be resumed when the HTTP server
  # supports byte ranges. If it cannot, restart cleanly instead.
  if [[ -s "$PART" ]]; then
    echo "[DOWNLOAD] Resuming interrupted download: $PART"
    if ! curl "${CURL_COMMON[@]}" --continue-at - \
      --remote-time --output "$PART" "$URL"; then
      echo '[DOWNLOAD] Resume unsupported or failed; restarting from byte zero.'
      rm -f -- "$PART"
      curl "${CURL_COMMON[@]}" --remote-time \
        --output "$PART" "$URL"
    fi
  else
    echo '[DOWNLOAD] Downloading South Korea extract to WSL native filesystem...'
    curl "${CURL_COMMON[@]}" --remote-time \
      --output "$PART" "$URL"
  fi
  if [[ ! -s "$PART" ]]; then
    echo '[ERROR] Download is empty. PBF has not been installed in cache.' >&2
    exit 4
  fi
  mv -f -- "$PART" "$DEST"
  echo "[DOWNLOAD] Complete: $DEST"
fi

# Count bytes without reading the (potentially gigabyte-scale) file into RAM.
stat -c '[INPUT] Cached PBF size: %s bytes' "$DEST"