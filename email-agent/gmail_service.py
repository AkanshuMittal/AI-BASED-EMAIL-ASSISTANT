"""Google OAuth (web flow) + Gmail API sending + mailto fallback.

Scope is gmail.send only: the app can send mail but cannot read the inbox.
Tokens live in st.session_state only (never in graph state, files or logs).
"""
import base64
import hashlib
import hmac
import mimetypes
import os
import secrets
import time
from email.message import EmailMessage
from urllib.parse import quote, urlencode

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Google may return scopes in a different order/form than requested.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
]


def _flow(cfg: dict) -> Flow:
    client_config = {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    # No PKCE: Streamlit loses session_state on the redirect, so a verifier can't be kept.
    # The client secret + signed `state` below protect the exchange instead.
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=cfg["redirect_uri"],
        autogenerate_code_verifier=False,
    )


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(secret: str) -> str:
    """Signed, time-limited state value (CSRF protection that survives the redirect)."""
    payload = f"{secrets.token_urlsafe(12)}.{int(time.time())}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_state(state: str, secret: str, max_age: int = 900) -> bool:
    try:
        nonce, ts, sig = state.split(".")
        ok = hmac.compare_digest(sig, _sign(f"{nonce}.{ts}", secret))
        return ok and (time.time() - int(ts)) <= max_age
    except Exception:
        return False


def auth_url(cfg: dict, state: str) -> str:
    url, _ = _flow(cfg).authorization_url(
        access_type="online", prompt="select_account", state=state
    )
    return url


def exchange_code(cfg: dict, code: str) -> dict:
    flow = _flow(cfg)
    flow.fetch_token(code=code)
    creds = flow.credentials
    info = (
        build("oauth2", "v2", credentials=creds, cache_discovery=False)
        .userinfo()
        .get()
        .execute()
    )
    return {
        "token": creds.token,
        "email": info.get("email", ""),
        "name": info.get("name") or info.get("email", ""),
        "picture": info.get("picture", ""),
    }


def revoke_token(token: str) -> None:
    try:
        requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=5,
        )
    except requests.RequestException:
        pass


def build_message(to, subject, body, cc=(), bcc=(), attachments=()) -> EmailMessage:
    """attachments: iterable of (filename, bytes, mime_type)."""
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data, mime in attachments:
        ctype = mime if mime and "/" in mime else (mimetypes.guess_type(name)[0] or "application/octet-stream")
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


def send_gmail(token: str, to: str, subject: str, body: str, cc=(), bcc=(), attachments=()) -> None:
    """Send from the signed-in user's own account. Raises on failure."""
    msg = build_message(to, subject, body, cc, bcc, attachments)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = build("gmail", "v1", credentials=Credentials(token=token), cache_discovery=False)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _query(params: dict) -> str:
    return urlencode({k: v for k, v in params.items() if v}, quote_via=quote)


def mailto_link(to: str, subject: str, body: str, cc=(), bcc=()) -> str:
    q = _query({"cc": ",".join(cc), "bcc": ",".join(bcc), "subject": subject, "body": body})
    return f"mailto:{quote(to, safe='@')}?{q}"


def gmail_compose_link(to: str, subject: str, body: str, cc=(), bcc=()) -> str:
    """Opens a Gmail compose window in the browser, no mail app needed."""
    q = _query({"view": "cm", "fs": "1", "to": to, "cc": ",".join(cc), "bcc": ",".join(bcc),
                "su": subject, "body": body})
    return f"https://mail.google.com/mail/?{q}"


def outlook_compose_link(to: str, subject: str, body: str, cc=(), bcc=()) -> str:
    q = _query({"to": to, "cc": ",".join(cc), "bcc": ",".join(bcc), "subject": subject, "body": body})
    return f"https://outlook.office.com/mail/deeplink/compose?{q}"