"""Worker-owned HTTP sessions with one shared monotonic budget per operation.

Requests timeouts cannot preempt DNS, OS stalls or a peer that trickles bytes.
Late results are rejected; this is not a hard real-time transport guarantee.
"""

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests
from urllib3.util import Timeout


class DeadlineExceeded(TimeoutError):
    pass


class DeadlineClient:
    ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
    TOKEN_REFRESH_MARGIN = timedelta(minutes=2)
    MAX_RETRIES = 3
    OPERATION_SECONDS = 4.0

    def __init__(self, base_url, username, password):
        self.base_url, self.username, self.password = base_url.rstrip("/"), username, password
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.access_token = self.refresh_token = self.token_expiry = None
        self._deadline = None
        self._owner = None

    @contextmanager
    def operation_budget(self, seconds=4.0):
        owner = threading.get_ident()
        if self._owner is None:
            self._owner = owner
        if self._owner != owner:
            raise RuntimeError("HTTP clients must not be shared between workers")
        previous = self._deadline
        self._deadline = min(previous, time.monotonic() + seconds) if previous else time.monotonic() + seconds
        try:
            yield
        finally:
            self._deadline = previous

    def remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("HTTP operation deadline expired")
        return remaining

    def deadline_expired(self):
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _single(self, method, endpoint, **kwargs):
        remaining = self.remaining()
        timeout = Timeout(total=remaining, connect=min(1.0, remaining), read=min(1.5, remaining))
        with self.session.request(method, self.base_url + endpoint, timeout=timeout, stream=True,
                                  allow_redirects=False, **kwargs) as response:
            self.remaining()
            if response.status_code == 401:
                return 401, None
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                return response.status_code, None
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                self.remaining()
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("HTTP response too large")
            data = json.loads(body)
            self.remaining()
            return response.status_code, data

    def login(self):
        with self.operation_budget(self.OPERATION_SECONDS):
            try:
                status, data = self._single("POST", "/token/login", auth=(self.username, self.password))
                if status != 200:
                    return False
                self.access_token, self.refresh_token = data["access_token"], data["refresh_token"]
                self.token_expiry = datetime.now(timezone.utc) + self.ACCESS_TOKEN_LIFETIME - self.TOKEN_REFRESH_MARGIN
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                return True
            except Exception:
                return False

    def _refresh_token(self):
        try:
            status, data = self._single("POST", "/token/refresh",
                                       headers={"Authorization": f"Bearer {self.refresh_token}"})
            if status == 200:
                self.access_token = data["access_token"]
                self.token_expiry = datetime.now(timezone.utc) + self.ACCESS_TOKEN_LIFETIME - self.TOKEN_REFRESH_MARGIN
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                return True
        except Exception:
            pass
        return self.login()  # Nested budget cannot extend the original deadline.

    def _ensure_authenticated(self):
        self.remaining()
        if not self.access_token:
            return self.login()
        if self.token_expiry and datetime.now(timezone.utc) >= self.token_expiry:
            return self._refresh_token()
        return True

    def _request(self, method, endpoint, **kwargs):
        with self.operation_budget(self.OPERATION_SECONDS):
            for attempt in range(self.MAX_RETRIES if method == "GET" else 1):
                try:
                    if not self._ensure_authenticated():
                        return None
                    status, data = self._single(method, endpoint, **kwargs)
                    if status == 401 and self._refresh_token():
                        status, data = self._single(method, endpoint, **kwargs)
                    return data if 200 <= status < 300 else None
                except Exception:
                    # Ambiguous command outcomes are never blindly retried.
                    if method != "GET" or attempt + 1 == self.MAX_RETRIES:
                        return None
                    try:
                        delay = min(0.1 * (2 ** attempt), self.remaining())
                        time.sleep(delay)
                        self.remaining()
                    except DeadlineExceeded:
                        return None
        return None

    def get(self, endpoint, **kwargs):
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint, **kwargs):
        return self._request("POST", endpoint, **kwargs)

    def execute_critical_command(self, command_name, method, endpoint, payload=None,
                                 expected_state=None, max_attempts=1, backoff_base=1.0):
        with self.operation_budget(self.OPERATION_SECONDS):
            response = self._request(method, endpoint, json=payload)
            if response is None:
                return False
            return expected_state is None or self.get_bot_state() == expected_state.lower()
