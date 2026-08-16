#!/usr/bin/env bash
# run.sh — usage: ./run.sh start|stop|status
cd "$(dirname "$0")"
PIDFILE=./run.pid
LOGFILE=./tracker.log

case "$1" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))"
      exit 1
    fi
    nohup python3 tracker.py "$2" "$3" "$4">> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "started (pid $!)"
    ;;
  stop)
    kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PIDFILE"))"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 start|stop|status"
    ;;
esac
