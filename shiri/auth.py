"""Local admin token authentication and short-lived browser sessions."""
import hashlib
import hmac
import secrets
import re
import time

SESSION_SECONDS = 12 * 60 * 60
COOKIE = "shiri_session"


class Auth:
    def __init__(self, token: str):
        if len(token) < 32:
            raise ValueError("Admin token must contain at least 32 characters; run shiri bootstrap")
        self._token = token
        self._session_key = hashlib.sha256(("shiri-browser-session:" + token).encode()).digest()

    def token_valid(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token.encode())

    def create_session(self) -> str:
        data = f"{int(time.time())}.{secrets.token_hex(16)}"
        signature = hmac.new(self._session_key, data.encode(), hashlib.sha256).hexdigest()
        return f"{data}.{signature}"

    def session_valid(self, value: str) -> bool:
        if len(value) > 160:
            return False
        try:
            issued, nonce, signature = value.split(".")
            age = time.time() - int(issued)
        except (ValueError, TypeError):
            return False
        if not 0 <= age <= SESSION_SECONDS or not re.fullmatch(r"[0-9a-f]{32}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            return False
        expected = hmac.new(self._session_key, f"{issued}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
