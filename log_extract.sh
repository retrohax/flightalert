#!/usr/bin/env bash
awk -v start="$1" -v end="$2" '
{
    ts = substr($0, 1, 19)
    if (ts >= start && ts <= end && (/DEBUG registration/ || /DEBUG No data returned/)) print
}
' tracker.log