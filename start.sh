#!/bin/sh
# Simple launcher for `mjpg_streamer` that works from any directory.
# Prefer `streamctl_service.py` for a persistent web UI + API.

# Directory containing this script (absolute).
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Streamer install / build directory.
if [ -n "${MJPG_STREAMER_ROOT}" ]; then
  STREAMER_ROOT="${MJPG_STREAMER_ROOT}"
else
  STREAMER_ROOT="${SCRIPT_DIR}"
fi

# Normalize to absolute path when MJPG_STREAMER_ROOT was relative.
case "${STREAMER_ROOT}" in
  /*) ;;
  *) STREAMER_ROOT=$(CDPATH= cd -- "${STREAMER_ROOT}" && pwd) ;;
esac

if [ ! -x "${STREAMER_ROOT}/mjpg_streamer" ]; then
  printf '%s\n' "start.sh: no executable ${STREAMER_ROOT}/mjpg_streamer" >&2
  printf '%s\n' "Set MJPG_STREAMER_ROOT to the folder built by 'make' (contains mjpg_streamer, *.so, www/)." >&2
  exit 1
fi

cd "${STREAMER_ROOT}" || exit 1

# Plugins are loaded from cwd-relative paths by default; also help dlopen().
export LD_LIBRARY_PATH="${STREAMER_ROOT}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

exec "${STREAMER_ROOT}/mjpg_streamer" "$@"
