from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import json
import logging

def rapidapi_request(api_url, api_host, api_key, registration: str):
	if not api_url or not api_host or not api_key or not registration:
		logging.error("Missing required parameters for RapidAPI request")
		return None
	url = f"{api_url.rstrip('/')}/registration/{registration}/"
	request = Request(url)
	request.add_header("x-rapidapi-key", api_key)
	request.add_header("x-rapidapi-host", api_host)
	request.add_header("accept", "application/json")

	try:
		with urlopen(request, timeout=20) as response:
			return json.load(response)
	except HTTPError as exc:
		logging.error("Failed to fetch snapshot for %s: HTTP %s %s", registration, exc.code, exc.reason)
	except URLError as exc:
		reason = getattr(exc, "reason", str(exc))
		logging.error("Failed to fetch snapshot for %s: network error: %s", registration, reason)
	except TimeoutError:
		logging.error("Failed to fetch snapshot for %s: request timed out", registration)
	except json.JSONDecodeError as exc:
		logging.error(
			"Failed to fetch snapshot for %s: invalid JSON at line %s column %s",
			registration,
			exc.lineno,
			exc.colno,
		)
	except Exception as exc:
		logging.error("Failed to fetch snapshot for %s: %s", registration, exc)

	return None
