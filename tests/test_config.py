from src.config import Settings


def test_webhook_retry_delays_parses_comma_separated_string():
    s = Settings(WEBHOOK_RETRY_DELAYS_MINUTES="5,10,15")
    assert s.webhook_retry_delays_minutes == [5, 10, 15]


def test_webhook_retry_delays_handles_whitespace():
    s = Settings(WEBHOOK_RETRY_DELAYS_MINUTES=" 5, 10 ,15 ")
    assert s.webhook_retry_delays_minutes == [5, 10, 15]


def test_webhook_retry_delays_empty_string_is_no_retries():
    s = Settings(WEBHOOK_RETRY_DELAYS_MINUTES="")
    assert s.webhook_retry_delays_minutes == []
