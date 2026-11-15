"""Wait for local API readiness; a real MCP smoke test is still required."""
import time

import httpx

for url in ['http://127.0.0.1:11335/health']:
    for attempt in range(120):
        try:
            r = httpx.get(url, timeout=3)
            r.raise_for_status()
            print(url, r.json())
            break
        except httpx.HTTPError:
            time.sleep(1)
    else:
        raise SystemExit('Readiness timeout: ' + url)
