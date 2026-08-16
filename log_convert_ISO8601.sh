#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 2 ]]; then
	echo "Usage: $0 <input_log> <output_log>" >&2
	exit 1
fi

input_log="$1"
output_log="$2"

if [[ ! -f "$input_log" ]]; then
	echo "Input file not found: $input_log" >&2
	exit 1
fi

python3 - "$input_log" "$output_log" <<'PY'
from __future__ import annotations

import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

input_path = sys.argv[1]
output_path = sys.argv[2]

# Matches log lines that begin with: YYYY-MM-DD HH:MM:SS
ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(.*)$")
source_tz = ZoneInfo("America/Chicago")

converted = 0
skipped = 0

with open(input_path, "r", encoding="utf-8") as src, open(output_path, "w", encoding="utf-8") as dst:
	for line in src:
		match = ts_pattern.match(line)
		if not match:
			dst.write(line)
			skipped += 1
			continue

		naive_dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
		aware_dt = naive_dt.replace(tzinfo=source_tz)
		iso_ts = aware_dt.isoformat(timespec="seconds")
		line_ending = "\n" if line.endswith("\n") else ""

		dst.write(f"{iso_ts}{match.group(2)}{line_ending}")
		converted += 1

print(f"Converted {converted} line(s); left {skipped} line(s) unchanged.")
PY
