import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
from test_refresh_source_metadata import inventory, metadata


def test_metadata_requests_overlap_with_a_fixed_bound_and_preserve_all_results(tmp_path):
    inventory(tmp_path, [f"github.com/demo/repo-{i}" for i in range(10)])
    lock = threading.Lock()
    active = peak = 0
    calls = []

    def handler(request):
        nonlocal active, peak
        with lock:
            calls.append(request.url.path)
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return httpx.Response(200, json={"name": request.url.path.split("/")[-1]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = metadata.refresh(
            tmp_path, client, workers=3, now=datetime(2026, 10, 14, tzinfo=UTC)
        )
    assert len(calls) == len(set(calls)) == 10
    assert 1 < peak <= 3
    assert all(row["http_status"] == 200 for row in result.values())


def test_later_throttle_stops_dispatch_but_keeps_completed_metadata(tmp_path):
    inventory(tmp_path, [f"github.com/demo/repo-{i:02}" for i in range(30)])
    calls = []
    lock = threading.Lock()

    def handler(request):
        with lock:
            calls.append(request.url.path)
        if request.url.path.endswith("repo-01"):
            return httpx.Response(429, headers={"retry-after": "120"})
        time.sleep(0.02)
        return httpx.Response(200, json={"name": request.url.path.split("/")[-1]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = metadata.refresh(tmp_path, client, workers=3)
    assert len(calls) <= 4  # One probe plus at most one bounded worker window.
    assert result["https://github.com/demo/repo-00"]["http_status"] == 200
    assert result["https://github.com/demo/repo-01"]["http_status"] == 429
    for url, row in result.items():
        if "/" + url.rsplit("/", 1)[-1] not in ["/" + call.rsplit("/", 1)[-1] for call in calls]:
            assert row == {}


@pytest.mark.parametrize("workers", [0, 9, -1])
def test_metadata_rejects_unbounded_worker_settings(tmp_path, workers):
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="WORKERS"):
            metadata.refresh(tmp_path, client, workers=workers)


def test_case_aliases_share_one_metadata_request(tmp_path):
    inventory(tmp_path, [
        "github.com/AAA/probe", "github.com/Demo/Patches", "github.com/demo/patches"
    ])
    calls = []

    def handler(request):
        calls.append(request.url.path)
        # A duplicate alias must not turn a successful lookup into a later failure.
        if request.url.path == "/repos/demo/patches":
            return httpx.Response(503)
        return httpx.Response(200, json={"name": "patches", "stargazers_count": 12})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = metadata.refresh(tmp_path, client, workers=3)
    assert len(calls) == 2
    assert len(result) == 2
    assert result["https://github.com/demo/patches"]["source_metadatum"]["repo_stars"] == 12
    assert result["https://github.com/demo/patches"]["http_status"] == 200
