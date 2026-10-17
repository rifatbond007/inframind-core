#!/bin/sh
CONTAINER_ID=$(cat /proc/self/cgroup | grep "kubepods" | head -1 | cut -d/ -f3)
echo "container id: $CONTAINER_ID"
echo "no. of open files in this process: $(ls /proc/self/fd 2>/dev/null | wc -l)"
echo "soft nofile limit: $(ulimit -Sn)"
echo "hard nofile limit: $(ulimit -Hn)"
echo "system-wide file-max: $(cat /proc/sys/fs/file-max 2>/dev/null)"
