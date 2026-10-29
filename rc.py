"""StoryBit Firebase Remote Config bootstrap.

The APK ships no endpoint or crypto key. Everything (API_URL / API_AI_URL /
ED / AI_ED / AED / MORE_APP_URL_ED) is published in Firebase Remote Config of
project `storybit-story-maker`, so the gateway pulls it at runtime instead of
hardcoding - vendor key rotation then costs us nothing.

Flow (verified live 2026-10-29):
  resources.arsc -> AIza* api key + google_app_id + project_id
  -> POST firebaseinstallations.googleapis.com/v1/projects/<proj>/installations
  -> POST firebaseremoteconfig.googleapis.com/v1/projects/<proj>/namespaces/firebase:fetch
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

PKG = "storybit.story.maker.animated.storymaker"
PROJECT_ID = "storybit-story-maker"
APP_ID = "1:89763144585:android:c44334848bf72e5469b758"
GOOGLE_API_KEY = "AIzaSyDuEwsBdKGPWJahPQ0ti87udLNgO-haeOs"
# signing cert SHA1 of the distributed (modded) APK - the package/cert pair the
# RC backend accepts for this app id.
APP_CERT_SHA1 = "2BFBA76612C78FE8675F7829457B5E47F26529C7"

_CTX = ssl.create_default_context()

# Values captured on 2026-10-29; used when Firebase is unreachable so the
# gateway still boots. Refreshed automatically when rc.fetch() succeeds.
FALLBACK = {
    "API_URL": "https://stb.snovxgames.com:2053",
    "API_AI_URL": "https://aa.snovxgames.com:5000",
    "MORE_APP_URL": "https://apps.snovxgames.com:7000",
    "ED": "ksdfjgnsjkdOLKfskjdfghJGFsrfdgad",
    "AI_ED": "LJs97MDf0EH667sjfh6gsUsjdf897jmj",
    "AED": "KJhdjkfb734OjdlKHKjJhdkgjlaj3480",
    "MORE_APP_URL_ED": "lksjfdlnalKjsdKLJghgdkhlaksdhgkS",
    "FN": "fDFRCqlZINt+cMkiYeL9cQ==",
}


def _post(url: str, body: dict, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read())


def _common_headers() -> dict:
    return {
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Android-Package": PKG,
        "X-Android-Cert": APP_CERT_SHA1,
        "User-Agent": "FirebaseInstallations/17.2.0",
    }


def fetch(timeout: int = 30) -> dict[str, str]:
    """Register an installation then pull the published RC template."""
    inst = _post(
        f"https://firebaseinstallations.googleapis.com/v1/projects/{PROJECT_ID}/installations",
        {"appId": APP_ID, "authVersion": "FIS_v2", "sdkVersion": "a:17.2.0"},
        _common_headers(), timeout,
    )
    body = {
        "app_instance_id": inst["fid"],
        "app_instance_id_token": inst["authToken"]["token"],
        "app_id": APP_ID,
        "app_version": "306",
        "app_build": "3.0.6",
        "country_code": "US",
        "language_code": "en",
        "platform_version": "35",
        "package_name": PKG,
        "sdk_version": "22101300",
        "time_zone": "UTC",
        "first_open_time": "2025-01-01T00:00:00Z",
        "analytics_user_properties": {},
    }
    url = (f"https://firebaseremoteconfig.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/namespaces/firebase:fetch?key={GOOGLE_API_KEY}")
    data = _post(url, body, _common_headers(), timeout)
    return {k: v for k, v in (data.get("entries") or {}).items() if isinstance(v, str)}


class Config:
    """Live view over the RC entries with a static fallback."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = dict(FALLBACK)
        self.source = "fallback"
        self.fetched_at = 0.0

    def refresh(self, timeout: int = 30) -> bool:
        try:
            entries = fetch(timeout)
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            return False
        if not entries:
            return False
        self.entries.update(entries)
        self.source = "remote-config"
        self.fetched_at = time.time()
        return True

    def get(self, key: str, default: str = "") -> str:
        return self.entries.get(key, default)

    @property
    def api_url(self) -> str:
        return self.get("API_URL", FALLBACK["API_URL"]).rstrip("/")

    @property
    def ai_url(self) -> str:
        return self.get("API_AI_URL", FALLBACK["API_AI_URL"]).rstrip("/")

    @property
    def more_url(self) -> str:
        return self.get("MORE_APP_URL", FALLBACK["MORE_APP_URL"]).rstrip("/")

    @property
    def key_main(self) -> bytes:
        return self.get("ED", FALLBACK["ED"]).encode()

    @property
    def key_ai(self) -> bytes:
        return self.get("AI_ED", FALLBACK["AI_ED"]).encode()

    @property
    def key_more(self) -> bytes:
        return self.get("MORE_APP_URL_ED", FALLBACK["MORE_APP_URL_ED"]).encode()
