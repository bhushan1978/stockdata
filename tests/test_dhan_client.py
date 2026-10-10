from datetime import datetime

import pytest

from conftest import make_client
from stockdata.config import Settings
from stockdata.dhan import (DhanAuthError, DhanClient, DhanPermanentError, DhanTransientError,
                            RateLimiter)
from stockdata.instruments import Instrument
from stockdata.timeframes import INTRADAY_15

INST = Instrument("NSE_EQ", 900001, "SIMALPHA", "EQUITY")


def payload(sec=900001):
    inst = Instrument("NSE_EQ", sec, "X", "EQUITY")
    return INTRADAY_15.payload(inst, datetime(2024, 5, 6), datetime(2024, 5, 8),
                               intraday_date_format="datetime", client_id=None)


def post(client):
    return client.post(INTRADAY_15.endpoint, payload())


def test_request_shape_matches_the_documented_fields(fake):
    client = make_client(fake)
    r = post(client)
    sent = fake.requests("/charts/intraday")[0]["payload"]
    assert sent == {"securityId": "900001", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                    "oi": False, "fromDate": "2024-05-06 00:00:00", "toDate": "2024-05-08 00:00:00",
                    "interval": 15}
    assert r.attempts == 1 and len(r.data["timestamp"]) == 50


def test_retries_5xx_with_backoff_then_succeeds(fake):
    fake.fail(times=2, status=500)
    client = make_client(fake)
    r = post(client)
    assert r.attempts == 3 and client.requests_sent == 3
    assert len(client.sleeps) == 2 and client.sleeps[1] >= client.sleeps[0] * 0.99  # exponential-ish
    assert all(0 < s <= 60 for s in client.sleeps)


def test_429_honours_retry_after_and_slows_the_limiter(fake):
    fake.fail(times=1, status=429, headers={"Retry-After": "3"},
              body={"errorType": "Rate_Limit", "errorCode": "DH-904", "errorMessage": "too many"})
    client = make_client(fake)
    r = post(client)
    assert r.attempts == 2 and client.sleeps == [3.0]
    assert client.limiter.penalty > 1.0


def test_connection_drop_is_retried(fake):
    fake.fail(times=1, status="drop")
    assert post(make_client(fake)).attempts == 2


def test_truncated_json_body_is_retried_not_trusted(fake):
    fake.fail(times=1, status="garbage")
    assert post(make_client(fake)).attempts == 2


def test_retries_are_bounded_then_transient_error_carries_attempts(fake):
    fake.fail(times=50, status=503)
    client = make_client(fake, max_attempts=3)
    with pytest.raises(DhanTransientError) as e:
        post(client)
    assert e.value.attempts == 3 and client.requests_sent == 3 and e.value.http_status == 503


def test_permanent_error_is_not_retried(fake):
    client = make_client(fake)
    with pytest.raises(DhanPermanentError) as e:
        client.post(INTRADAY_15.endpoint, payload(sec=111))   # unknown security
    assert e.value.error_code == "DH-905" and client.requests_sent == 1 and client.sleeps == []


def test_auth_error_is_not_retried(fake):
    client = make_client(fake, token="wrong")
    with pytest.raises(DhanAuthError) as e:
        post(client)
    assert e.value.http_status == 401 and client.requests_sent == 1


def test_token_never_appears_in_repr_or_errors(fake):
    client = make_client(fake, token="SECRET-TOKEN-123")
    assert "SECRET" not in repr(client)
    with pytest.raises(DhanAuthError) as e:
        post(client)
    assert "SECRET" not in str(e.value) and "SECRET" not in repr(e.value)
    assert "SECRET" not in repr(Settings(access_token="SECRET-TOKEN-123", client_id="CID"))


def test_rate_limiter_spaces_requests_evenly():
    now = [100.0]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        now[0] += s

    lim = RateLimiter(4, clock=lambda: now[0], sleep=fake_sleep)   # 4/s -> 0.25 s apart
    for _ in range(5):
        lim.wait()
    assert sleeps == pytest.approx([0.25] * 4)


def test_token_refused_over_plain_http_to_a_remote_host():
    s = Settings(access_token="t", base_url="http://example.com/v2")
    with pytest.raises(SystemExit):
        s.require_token()
    Settings(access_token="t", base_url="http://127.0.0.1:1/v2").require_token()
    Settings(access_token="t").require_token()


TOKEN_BODY = {"errorType": "Input_Exception", "errorCode": "DH-906", "errorMessage": "Invalid Token"}


def test_dh_906_invalid_token_is_an_authentication_failure_and_is_not_retried(fake):
    """Seen on a real account: a rejected token comes back as HTTP 400 DH-906 'Invalid Token', not as 401/DH-901."""
    fake.fail(times=5, status=400, body=TOKEN_BODY)
    client = make_client(fake)
    with pytest.raises(DhanAuthError) as e:
        post(client)
    assert e.value.http_status == 400 and e.value.error_code == "DH-906" and "Invalid Token" in e.value.message
    assert client.requests_sent == 1 and client.sleeps == []


def test_a_dh_906_that_does_not_name_the_token_stays_a_permanent_request_error(fake):
    fake.fail(times=1, status=400, body={"errorType": "Order_Error", "errorCode": "DH-906", "errorMessage": "Order rejected"})
    client = make_client(fake)
    with pytest.raises(DhanPermanentError) as e:
        post(client)
    assert e.value.error_code == "DH-906" and not isinstance(e.value, DhanAuthError)
