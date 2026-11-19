#!/usr/bin/env bash
# wq#5028: install the daily fact-taxonomy scan as a lumen USER timer (Linger=yes on Lumen;
# no root needed). --verify checks the installed copies match and the timer is enabled.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/home/lumen/.config/systemd/user
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
UNITS=(lumen-fact-taxonomy.service lumen-fact-taxonomy.timer)
if [ "${1:-}" = "--verify" ]; then
  rc=0
  for u in "${UNITS[@]}"; do cmp -s "$HERE/$u" "$DEST/$u" && echo "OK   $u" || { echo "DIFF $u"; rc=1; }; done
  systemctl --user is-enabled lumen-fact-taxonomy.timer >/dev/null && echo "OK   timer enabled" || { echo "FAIL timer not enabled"; rc=1; }
  systemctl --user list-timers lumen-fact-taxonomy.timer --no-legend | sed 's/^/     /'
  exit $rc
fi
mkdir -p "$DEST"
for u in "${UNITS[@]}"; do
  [ -e "$DEST/$u" ] && cp -p "$DEST/$u" "$DEST/$u.bak.$(date +%Y%m%d_%H%M%S)"
  install -m 0644 "$HERE/$u" "$DEST/$u"; echo "installed $DEST/$u"
done
systemctl --user daemon-reload
systemctl --user enable --now lumen-fact-taxonomy.timer
"$0" --verify
