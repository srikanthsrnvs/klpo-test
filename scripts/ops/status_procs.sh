#!/bin/sh
echo "procs: chrome=$(pgrep -fc '[g]oogle-chrome') daemons=$(pgrep -fc 'browser_harness[.]daemon') tool_servers=$(pgrep -fc 'jev_browser[.]toolset') shared_server=$(pgrep -fc 'serve_tools[.]py') pi=$(pgrep -fc '[p]i-acp') rl=$(pgrep -fc '[r]l @ /workspace') pids=$(cat /sys/fs/cgroup/pids.current)"
