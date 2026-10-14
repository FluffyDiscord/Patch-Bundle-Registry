import io
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from scripts import cloudflare_catalog as cloud
from scripts import cloudflare_storage as storage


class S3Failure(Exception):
    """Use botocore's public error-response shape without requiring the SDK in CI."""

    def __init__(self, code, status):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, wall=1_800_000_000, sleeps=[])

    def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay

    monkeypatch.setattr(storage.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(storage.time, "time", lambda: state.wall + state.now)
    monkeypatch.setattr(storage.time, "sleep", sleep)
    monkeypatch.setattr(storage.random, "uniform", lambda *args: 0)
    return state


@pytest.mark.parametrize(
    ("code", "status"),
    [("ServiceUnavailable", 503), ("SlowDown", 503), ("InternalError", 500), ("Unknown", 429)],
)
def test_r2_reads_retry_transient_responses_and_close_successful_body(clock, code, status):
    body = io.BytesIO(b"verified data")
    requests = []

    def get_object(**kwargs):
        requests.append(kwargs)
        if len(requests) < 3:
            raise S3Failure(code, status)
        return {"Body": body}

    client = cloud.Cloud.__new__(cloud.Cloud)
    client.bucket = "test"
    client.s3 = SimpleNamespace(get_object=lambda **kwargs: pytest.fail("Used write client"))
    client.s3_reads = SimpleNamespace(get_object=get_object)
    client._r2_reader = storage.R2Reader(client.read_object)
    assert client.get("objects/example") == b"verified data"
    assert body.closed
    assert requests == [{"Bucket": "test", "Key": "objects/example"}] * 3
    assert clock.sleeps == [2, 4]
    assert client._r2_reader.retries == 2


@pytest.mark.parametrize(
    "error",
    [S3Failure("AccessDenied", 403), S3Failure("NoSuchKey", 404), ValueError("Invalid request")],
)
def test_r2_reads_do_not_retry_permanent_or_programming_errors(clock, error):
    requests = []

    def get(key):
        requests.append(key)
        raise error

    reader = storage.R2Reader(get)
    with pytest.raises(type(error)) as raised:
        reader.read("objects/example")
    assert raised.value is error
    assert requests == ["objects/example"]
    assert clock.sleeps == []


def test_r2_read_retries_are_bounded(clock):
    error = S3Failure("ServiceUnavailable", 503)
    requests = []

    def get(key):
        requests.append(key)
        raise error

    reader = storage.R2Reader(get)
    with pytest.raises(S3Failure) as raised:
        reader.read("objects/example")
    assert raised.value is error
    assert len(requests) == 3
    assert clock.sleeps == [2, 4]


def test_r2_stream_failure_closes_body_before_retry(clock):
    class BrokenBody(io.BytesIO):
        def read(self):
            raise ConnectionError("Interrupted response")

    broken = BrokenBody()
    good = io.BytesIO(b"complete")
    bodies = iter([broken, good])
    client = cloud.Cloud.__new__(cloud.Cloud)
    client.bucket = "test"
    client.s3_reads = SimpleNamespace(get_object=lambda **kwargs: {"Body": next(bodies)})
    client._r2_reader = storage.R2Reader(client.read_object, transport_errors=(ConnectionError,))
    assert client.get("objects/example") == b"complete"
    assert broken.closed and good.closed
    assert clock.sleeps == [2]


def test_r2_read_concurrency_is_capped_even_with_more_transfer_workers():
    guard = Lock()
    active, peak = 0, 0

    def get(key):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return key

    reader = storage.R2Reader(get, workers=16)
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(reader.read, range(40))) == list(range(40))
    assert 1 < peak <= 4


def test_r2_throttle_cooldown_applies_to_other_read_workers(monkeypatch):
    now = [0.0]
    first_wait, both_wait, resume = Event(), Event(), Event()
    guard = Lock()
    waits, requests = [], []

    def sleep(delay):
        with guard:
            waits.append(delay)
            first_wait.set()
            if len(waits) == 2:
                both_wait.set()
        assert resume.wait(5)

    def get(key):
        with guard:
            requests.append(key)
            if key == "hot" and requests.count("hot") == 1:
                raise S3Failure("ServiceUnavailable", 503)
        return key

    monkeypatch.setattr(storage.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(storage.time, "sleep", sleep)
    monkeypatch.setattr(storage.random, "uniform", lambda *args: 0)
    reader = storage.R2Reader(get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reader.read, "hot")
        try:
            assert first_wait.wait(5)
            second = pool.submit(reader.read, "other")
            assert both_wait.wait(5)
            assert requests == ["hot"]
        finally:
            now[0] = 10.0
            resume.set()
        assert first.result(timeout=5) == "hot"
        assert second.result(timeout=5) == "other"
    assert waits == [2, 2]


@pytest.mark.parametrize("recover", [True, False])
def test_publication_activates_only_after_r2_read_verification(
    tmp_path, monkeypatch, clock, recover
):
    from test_cloudflare_catalog import MemoryCloud, archive, catalog, release

    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    previous = memory.active()
    previous_objects = set(memory.objects)
    archive(tmp_path, [release(1, body="New release notes")])
    original_get = memory.get
    target = []
    attempts = []
    guard = Lock()

    def get(key):
        if key.startswith("objects/") and key not in previous_objects:
            with guard:
                if not target:
                    target.append(key)
                if key == target[0]:
                    attempts.append(key)
                    if not recover or len(attempts) < 3:
                        raise S3Failure("ServiceUnavailable", 503)
        return original_get(key)

    reader = storage.R2Reader(get)
    monkeypatch.setattr(memory, "get", reader.read)
    del memory._transfers
    if recover:
        memory.publish()
        assert memory.active() != previous
        memory.verify_database(memory.active()["seq"], memory.document(memory.active()))
    else:
        with pytest.raises(S3Failure):
            memory.publish()
        assert memory.active() == previous
    assert len(attempts) == 3
    assert reader.retries == 2


@pytest.mark.parametrize("date", [False, True])
def test_r2_reads_obey_retry_after(clock, date):
    from email.utils import formatdate

    error = S3Failure("ServiceUnavailable", 503)
    header = formatdate(clock.wall + 10, usegmt=True) if date else "10"
    error.response["ResponseMetadata"]["HTTPHeaders"] = {"retry-after": header}
    requests = []

    def get(key):
        requests.append(clock.now)
        if len(requests) == 1:
            raise error
        return b"complete"

    assert storage.R2Reader(get).read("objects/example") == b"complete"
    assert requests == [0, 10]
    assert clock.sleeps == [10]



@pytest.mark.parametrize(
    ("header", "expected_delay"),
    [
        ("invalid", 2),
        ("٣٠", 2),
        ("0", 2),
        ("past", 2),
        ("60", 60),
        ("61", None),
        ("long-date", None),
    ],
)
def test_r2_retry_after_fallbacks_and_wait_budget(clock, header, expected_delay):
    from email.utils import formatdate

    if header == "past":
        header = formatdate(clock.wall - 30, usegmt=True)
    elif header == "long-date":
        header = formatdate(clock.wall + 61, usegmt=True)
    error = S3Failure("ServiceUnavailable", 503)
    error.response["ResponseMetadata"]["HTTPHeaders"] = {"retry-after": header}
    requests = []

    def get(key):
        requests.append(clock.now)
        if len(requests) == 1:
            raise error
        return b"complete"

    reader = storage.R2Reader(get)
    if expected_delay is None:
        with pytest.raises(S3Failure) as raised:
            reader.read("objects/example")
        assert raised.value is error
        assert requests == [0]
        assert clock.sleeps == []
    else:
        assert reader.read("objects/example") == b"complete"
        assert requests == [0, expected_delay]
        assert clock.sleeps == [expected_delay]



def test_cloud_get_client_disables_sdk_retries_without_changing_write_retries(monkeypatch):
    import sys
    from types import ModuleType

    package = ModuleType("botocore")
    package.__path__ = []
    config = ModuleType("botocore.config")
    config.Config = lambda **options: SimpleNamespace(**options)
    exceptions = ModuleType("botocore.exceptions")
    for name in (
        "ConnectionClosedError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "IncompleteReadError",
        "ReadTimeoutError",
        "ResponseStreamingError",
    ):
        setattr(exceptions, name, ConnectionError)
    package.config = config
    package.exceptions = exceptions
    for name, module in (
        ("botocore", package),
        ("botocore.config", config),
        ("botocore.exceptions", exceptions),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda service, **options: SimpleNamespace(**options)),
    )
    for name in (
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, "test")
    client = cloud.Cloud()
    try:
        assert client.s3_reads is not client.s3
        assert client.s3_reads.config.retries == {"total_max_attempts": 1, "mode": "standard"}
        assert client.s3.config.retries == {"max_attempts": 5, "mode": "standard"}
    finally:
        client.http.close()
