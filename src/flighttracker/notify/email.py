"""Render and send emails.

Templates use Jinja2 with autoescaping on: airline and seller names come from third-party APIs and
are treated as untrusted. Sending goes through Resend's HTTPS API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
from jinja2 import Environment, PackageLoader, StrictUndefined

from flighttracker.db.repositories import WatchDigest

RESEND_URL = "https://api.resend.com/emails"

_env = Environment(
    loader=PackageLoader("flighttracker.notify", "templates"),
    # Escape everything except plain-text templates (fail safe: unknown names are escaped).
    autoescape=lambda name: name is None or not name.endswith(".txt.j2"),  # noqa: S701
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def dollars(cents: int | None) -> str:
    return "n/a" if cents is None else f"${cents / 100:,.0f}"


_env.filters["dollars"] = dollars


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    html: str
    text: str


def render_digest(to: str, digests: list[WatchDigest], today: date) -> Email:
    context = {"digests": digests, "today": today}
    return Email(
        to=to,
        subject=f"Flight digest {today.isoformat()}: {len(digests)} watch(es)",
        html=_env.get_template("digest.html.j2").render(**context),
        text=_env.get_template("digest.txt.j2").render(**context),
    )


def send_via_resend(email: Email, *, api_key: str, sender: str, timeout: float = 20.0) -> str:
    response = httpx.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": sender,
            "to": [email.to],
            "subject": email.subject,
            "html": email.html,
            "text": email.text,
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Resend returned HTTP {response.status_code}")
    return str(response.json().get("id", ""))
