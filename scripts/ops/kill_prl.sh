#!/bin/sh
# Kill prime-rl's PRL:: worker processes but spare the dashboard (PRL::Dashboard), which the plain
# `pkill -9 -f "PRL::"` the run scripts used to do also took down.
for p in $(pgrep -f "PRL::" 2>/dev/null); do
  tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "Dashboard" || kill -9 "$p" 2>/dev/null
done
