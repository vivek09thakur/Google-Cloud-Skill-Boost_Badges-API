#!/usr/bin/env python3
"""
skillboost_api.py - read-only JSON API over Google Cloud Skills Boost public profiles.

No login needed: public profile pages are server-rendered HTML, so we just fetch
and parse them.

Setup:
  pip install fastapi uvicorn beautifulsoup4

Run:
  uvicorn skillboost_api:app --reload

Endpoints:
  GET /profile/{profile_id}            -> name, avatar, member_since, badges[]
  GET /profile/{profile_id}/badges     -> badges[] only
  Docs at http://127.0.0.1:8000/docs

CLI (no server):
  python skillboost_api.py 49e66b41-798d-4bbf-860e-35a9040ac66d
"""

import datetime as dt
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

BASE_URL = "https://www.skills.google/public_profiles/"
# The site returns 403 for non-browser User-Agents.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
PROFILE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Badge(BaseModel):
    id: Optional[int]
    title: str
    earned_at: Optional[str]  # ISO date (YYYY-MM-DD)
    earned_raw: str
    image_url: Optional[str]
    url: Optional[str]


class Profile(BaseModel):
    profile_id: str
    name: Optional[str]
    avatar_url: Optional[str]
    member_since: Optional[int]
    badge_count: int
    badges: list[Badge]


def fetch_html(profile_id: str) -> str:
    req = urllib.request.Request(BASE_URL + profile_id, headers=HEADERS)
    # Uses the OS trust store (works behind corporate TLS proxies, unlike certifi).
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_earned_date(text: str) -> Optional[str]:
    m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", text)
    if not m:
        return None
    try:
        return dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def parse_profile(profile_id: str, html: str) -> Profile:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one("main h1")
    avatar = soup.select_one("ql-avatar.profile-avatar")
    member_p = soup.select_one("main p.ql-body-large")
    member_since = None
    if member_p:
        m = re.search(r"\d{4}", member_p.get_text())
        member_since = int(m.group()) if m else None

    badges: list[Badge] = []
    for card in soup.select("div.profile-badge"):
        link = card.select_one("a.badge-image")
        img = link.select_one("img") if link else None
        title_el = card.select_one("span.ql-title-medium")
        earned_el = card.select_one("span.ql-body-medium")

        href = link["href"] if link and link.has_attr("href") else None
        badge_id = None
        if href:
            m = re.search(r"/badges/(\d+)", href)
            badge_id = int(m.group(1)) if m else None

        earned_raw = earned_el.get_text(strip=True) if earned_el else ""
        badges.append(
            Badge(
                id=badge_id,
                title=title_el.get_text(strip=True) if title_el else "",
                earned_at=parse_earned_date(earned_raw),
                earned_raw=earned_raw,
                image_url=img["src"] if img and img.has_attr("src") else None,
                url=href,
            )
        )

    return Profile(
        profile_id=profile_id,
        name=h1.get_text(strip=True) if h1 else None,
        avatar_url=avatar["src"] if avatar and avatar.has_attr("src") else None,
        member_since=member_since,
        badge_count=len(badges),
        badges=badges,
    )


@lru_cache(maxsize=256)
def get_profile(profile_id: str) -> Profile:
    return parse_profile(profile_id, fetch_html(profile_id))


def validate_id(profile_id: str) -> str:
    pid = profile_id.strip().lower()
    if not PROFILE_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="profile_id must be a UUID")
    return pid


app = FastAPI(title="Skills Boost Public Profile API", version="1.0.0")


@app.get("/profile/{profile_id}", response_model=Profile)
def profile(profile_id: str) -> Profile:
    pid = validate_id(profile_id)
    try:
        return get_profile(pid)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail="Profile not found or not public")
        raise HTTPException(status_code=502, detail=f"Upstream returned {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Upstream unreachable: {e.reason}")


@app.get("/profile/{profile_id}/badges", response_model=list[Badge])
def badges(profile_id: str) -> list[Badge]:
    return profile(profile_id).badges


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python skillboost_api.py <profile_id>")
    print(json.dumps(get_profile(validate_id(sys.argv[1])).model_dump(), indent=2))
