"""
Throwaway verification script for app/gtfs_api.py (issue #14).

Not part of the app's test suite — a one-off check that the client behaves correctly against
the real deployed GTFS service, using a disruption already confirmed in this app's own
scan_results (route 1, CLJ->LRD, 2026-08-30). Safe to delete once #14 is merged and confidence
is established; kept under scripts/ in the meantime per the issue's own wording.

Run: python3 scripts/verify_gtfs_api.py
"""

import http.server
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import gtfs_api  # noqa: E402

DISRUPTED_DATE = "2026-08-30"
NORMAL_DATE = "2026-08-23"


def count_direct(journeys: list[dict]) -> int:
    return sum(1 for j in journeys if j.get("kind") == "direct")


def main() -> int:
    failures = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    # Positive case: known-disrupted date should show zero direct journeys.
    result = gtfs_api.fetch_gtfs_journeys("CLJ", "LRD", DISRUPTED_DATE, "12:00", window_minutes=120)
    check("disrupted-date call succeeds", result.ok, result.error or "")
    direct = count_direct(result.journeys) if result.ok else -1
    check(f"disrupted-date ({DISRUPTED_DATE}) has 0 direct journeys", direct == 0, f"got {direct}")

    # Control case: known-normal Saturday should show at least one direct journey.
    result = gtfs_api.fetch_gtfs_journeys("CLJ", "LRD", NORMAL_DATE, "12:00", window_minutes=120)
    check("normal-date call succeeds", result.ok, result.error or "")
    direct = count_direct(result.journeys) if result.ok else -1
    check(f"normal-date ({NORMAL_DATE}) has >=1 direct journey", direct >= 1, f"got {direct}")

    # Negative case: unreachable host should return ok=False, not raise.
    original_base_url = gtfs_api._BASE_URL
    gtfs_api._BASE_URL = "http://127.0.0.1:1"
    try:
        result = gtfs_api.fetch_gtfs_journeys("CLJ", "LRD", NORMAL_DATE, "12:00")
        check("unreachable host returns ok=False", result.ok is False, f"error={result.error}")
    finally:
        gtfs_api._BASE_URL = original_base_url

    # Negative case: malformed/non-JSON 200 response. A tiny local server stands in for this
    # since the real GTFS service doesn't have a convenient endpoint that returns 200 + non-JSON.
    class _MalformedHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not json</html>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _MalformedHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    original_base_url = gtfs_api._BASE_URL
    gtfs_api._BASE_URL = f"http://127.0.0.1:{server.server_port}"
    try:
        result = gtfs_api.fetch_gtfs_journeys("CLJ", "LRD", NORMAL_DATE, "12:00")
        check(
            "malformed response returns ok=False, error=malformed_response",
            result.ok is False and result.error == "malformed_response",
            f"ok={result.ok} error={result.error}",
        )
    finally:
        gtfs_api._BASE_URL = original_base_url
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
