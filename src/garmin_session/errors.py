"""Typed 401 detection for garminconnect.

``Client._run_request`` raises a bare ``GarminConnectConnectionError`` carrying
``f"API Error {status}"`` for every >=400 response, 401 included. Only the
high-level ``Garmin.connectapi()`` / download wrappers carry the
``@_handle_api_errors`` decorator that translates 401 into
``GarminConnectAuthenticationError`` — the body-composition upload path
(``Client.post`` -> ``_run_request``) bypasses it entirely.

A caller that only drops its cached session on ``GarminConnectAuthenticationError``
therefore never recovers from a token that was rotated away by a peer process:
the poisoned client stays cached and every later call fails identically until
the process restarts.

A sibling service works around this by regex-matching ``"API Error 401"`` in the
exception message. That couples recovery to two things that can change without
warning — Garmin's error text and this library's f-string — and fails silently
if either moves. This module keys off the actual HTTP status instead: each
client's API session records the status of the last response it saw, and
``_run_request`` is wrapped to consult that recorded value.

The real fix belongs upstream (decorate ``_run_request``, or raise the auth
error directly on 401). When that lands, delete this module.
"""

import threading

from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError
from garminconnect.client import Client

_LAST_STATUS_ATTR = "_gss_last_http_status"
_INSTRUMENTED_ATTR = "_gss_instrumented"

_install_lock = threading.Lock()
_installed = False


def instrument(garmin) -> None:
    """Record every API response's status code on the underlying Client.

    ``_api_session`` is a plain ``requests.Session`` instance attribute, so
    wrapping ``.request`` once per client sticks for the client's lifetime.
    Call this before the client issues any request.
    """
    inner = garmin.client
    session = inner._api_session
    if getattr(session, _INSTRUMENTED_ATTR, False):
        return

    original_request = session.request

    def recording_request(*args, **kwargs):
        response = original_request(*args, **kwargs)
        setattr(inner, _LAST_STATUS_ATTR, getattr(response, "status_code", None))
        return response

    session.request = recording_request
    setattr(session, _INSTRUMENTED_ATTR, True)


def install() -> None:
    """Patch ``Client._run_request`` to raise the auth error on a real 401.

    Idempotent — safe to call from every session construction.
    """
    global _installed
    with _install_lock:
        if _installed:
            return

        original_run_request = Client._run_request

        def _run_request(self, method, path, **kwargs):
            setattr(self, _LAST_STATUS_ATTR, None)
            try:
                return original_run_request(self, method, path, **kwargs)
            except GarminConnectConnectionError as exc:
                if getattr(self, _LAST_STATUS_ATTR, None) == 401:
                    raise GarminConnectAuthenticationError(
                        f"Garmin rejected the session (HTTP 401): {exc}"
                    ) from exc
                raise

        Client._run_request = _run_request
        _installed = True
