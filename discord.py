import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def send_discord_message(webhook_url, title, color, url=None, fields=None, timestamp=None):
	embed = {"title": title, "color": color, "footer": {"text": "ADS-B Exchange"}}
	if url:
		embed["url"] = url
	if fields:
		embed["fields"] = fields
	if timestamp:
		embed["timestamp"] = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
	payload = json.dumps({"embeds": [embed]}).encode()
	if not webhook_url:
		logging.debug("Discord webhook URL not set; skipping notification: %s", json.dumps(embed, indent=2))
		return
	max_attempts = 6
	retry_delay_seconds = 10

	for attempt in range(1, max_attempts + 1):
		req = Request(webhook_url, data=payload, method="POST")
		req.add_header("Content-Type", "application/json")
		req.add_header("Accept", "application/json")
		req.add_header("User-Agent", "flightalert/1.0")
		try:
			with urlopen(req, timeout=10):
				logging.debug("Discord webhook post successful")
				return
		except HTTPError as exc:
			logging.warning(
				"Discord webhook post attempt failed HTTP %s (%s)",
				exc.code,
				exc.reason,
			)
		except (URLError, TimeoutError) as exc:
			reason = getattr(exc, "reason", str(exc))
			logging.warning(
				"Discord webhook post attempt failed due to network error: %s",
				reason,
			)
		except Exception as exc:
			logging.warning(
				"Discord webhook post attempt failed: %s",
				exc
			)

		if attempt < max_attempts:
			logging.debug("Retrying Discord webhook post in %ss", retry_delay_seconds)
			time.sleep(retry_delay_seconds)

	logging.error(
		"Discord webhook post failed after %s attempts; skipping notification",
		max_attempts
	)
