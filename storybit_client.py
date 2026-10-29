"""Async StoryBit v3.0.6 upstream client.

Wire format (reversed from com.snovxgames / storybit.story.maker.animated.storymaker):

  POST <host><path>?cc=&version_name=3.0.6&version_code=140
                   &andorid_os_version=35&package_name=<pkg>&installer=<installer>
  Content-Type: application/json
  {"data": base64(AES-256-CBC(pkcs5, zero-IV, <json payload>, key))}

  response {"data": base64(...)} -> same AES, same key

  Three hosts, three keys:
    stb.snovxgames.com:2053   catalog/payment/notification   key = RC "ED"
    aa.snovxgames.com:5000    AI (effects / gen_i / status)   key = RC "AI_ED"
    apps.snovxgames.com:7000  cross-promo banners             key = RC "MORE_APP_URL_ED"

  The AI host takes multipart/form-data instead of a JSON body: a text part
  named "data" (same AES blob, AI key) plus one raw file part whose field name
  is the local file name (okhttp MultipartBody.Part(name = file.getName())).
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from rc import Config, PKG

VERSION_NAME = "3.0.6"
VERSION_CODE = 140
UA = "okhttp/4.12.0"  # Cloudflare (error 1010) rejects anything else on these hosts
IV = b"\x00" * 16


class UpstreamError(RuntimeError):
    """Upstream answered with a business error rather than a payload."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class Job:
    job_id: str
    state: str
    done: bool
    url: str

    @property
    def raw(self) -> dict:
        return {"job_id": self.job_id, "state": self.state, "done": self.done, "url": self.url}


def aes_encrypt(obj: dict, key: bytes) -> str:
    cipher = AES.new(key, AES.MODE_CBC, IV)
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.b64encode(cipher.encrypt(pad(raw, 16))).decode()


def aes_decrypt(blob: str, key: bytes) -> dict:
    cipher = AES.new(key, AES.MODE_CBC, IV)
    return json.loads(unpad(cipher.decrypt(base64.b64decode(blob)), 16).decode())


class StoryBitClient:
    """Thin transport over the app's private API. No signatures, no device id,
    no session: every call is stateless."""

    def __init__(self, cfg: Config, timeout: float = 45.0, retries: int = 2):
        self.cfg = cfg
        self.timeout = timeout
        self.retries = retries
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"User-Agent": UA},
            verify=False,  # hosts serve a valid cert; kept permissive for proxies
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- plumbing

    def _query(self, cc: str, installer: str) -> str:
        return (f"cc={cc}&version_name={VERSION_NAME}&version_code={VERSION_CODE}"
                f"&andorid_os_version=35&package_name={PKG}&installer={installer}")

    async def _request_json(self, method: str, url: str, body: bytes | None,
                            headers: dict, timeout: float | None = None) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await self._http.request(method, url, content=body, headers=headers,
                                             timeout=timeout or self.timeout)
                if r.status_code == 403 and "1010" in r.text:
                    raise UpstreamError("cf-1010", "blocked by Cloudflare UA rule")
                r.raise_for_status()
                return json.loads(r.content)
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt == self.retries:
                    break
                await asyncio.sleep(0.6 * (attempt + 1))
        raise UpstreamError("network", str(last))

    async def call(self, path: str, payload: dict | None = None, *, host: str = "main",
                   cc: str = "en_US", installer: str = "com.android.vending",
                   timeout: float | None = None) -> dict:
        """POST a JSON request to one of the three hosts and return plaintext."""
        if not path.startswith("/"):
            path = "/" + path
        base, key = {
            "main": (self.cfg.api_url, self.cfg.key_main),
            "ai": (self.cfg.ai_url, self.cfg.key_ai),
            "more": (self.cfg.more_url, self.cfg.key_more),
        }[host]
        url = f"{base}{path}?{self._query(cc, installer)}"
        body = json.dumps({"data": aes_encrypt(payload or {}, key)}).encode()
        headers = {"Content-Type": "application/json"}
        return await self._decoded(url, body, headers, key, timeout)

    async def _decoded(self, url: str, body: bytes, headers: dict, key: bytes,
                       timeout: float | None = None) -> dict:
        raw = await self._request_json("POST", url, body, headers, timeout)
        blob = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(blob, str):
            return raw
        try:
            out = aes_decrypt(blob, key)
        except ValueError:
            return raw
        if isinstance(out, dict) and out.get("code") and out.get("msg"):
            raise UpstreamError(str(out["code"]), str(out["msg"]))
        return out

    # ------------------------------------------------------------------- AI API

    async def call_ai_multipart(self, path: str, payload: dict, image: bytes,
                                file_name: str = "image.jpg") -> dict:
        """POST multipart to the AI host: text part `data` + raw image part."""
        if not path.startswith("/"):
            path = "/" + path
        boundary = "----okhttp" + uuid.uuid4().hex
        enc = aes_encrypt(payload, self.cfg.key_ai)
        chunks: list[bytes] = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="data"\r\n'
            f"Content-Type: text/plain; charset=UTF-8\r\n\r\n{enc}\r\n".encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_name}"; '
            f'filename="{file_name}"\r\nContent-Type: multipart/form-data\r\n\r\n'.encode(),
            image, b"\r\n", f"--{boundary}--\r\n".encode(),
        ]
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "cc": "en_US", "version_name": VERSION_NAME, "version_code": str(VERSION_CODE),
            "andorid_os_version": "35", "package_name": PKG, "platform": "Android",
        }
        url = f"{self.cfg.ai_url}{path}"
        raw = await self._request_json("POST", url, b"".join(chunks), headers)
        blob = raw.get("data") if isinstance(raw, dict) else None
        if isinstance(blob, str):
            try:
                out = aes_decrypt(blob, self.cfg.key_ai)
            except ValueError:
                return raw
            if isinstance(out, dict) and out.get("code") and out.get("msg"):
                raise UpstreamError(str(out["code"]), str(out["msg"]))
            return out
        return raw

    # --------------------------------------------------------------- endpoints

    async def categories(self) -> dict:
        return await self.call("/android/v2/category/getEnableCategory", {"v": VERSION_CODE, "cc": "en_US"})

    async def explore(self) -> dict:
        return await self.call("/android/v2/template/getExploreData", {"v": VERSION_CODE, "cc": "en_US"})

    async def home_templates(self, cat_id: str, page: int = 1, limit: int = 20) -> dict:
        return await self.call("/android/v1/template/getHomeTemplates",
                               {"v": VERSION_CODE, "cc": "en_US", "cat_id": cat_id,
                                "page": page, "limit": limit})

    async def templates_by_category(self, cat_id: str, page: int = 1, limit: int = 20) -> dict:
        return await self.call("/android/v2/template/getAllTemplatesByCategoryExplore",
                               {"v": VERSION_CODE, "cc": "en_US", "cat_id": cat_id,
                                "page": page, "limit": limit})

    async def template_detail(self, temp_id: str) -> dict:
        return await self.call("/android/v2/template/getTemplateDetail",
                               {"v": VERSION_CODE, "temp_id": temp_id})

    async def dashboard_banners(self) -> dict:
        return await self.call("/android/v1/banner/getDashboardBanner", {"v": VERSION_CODE})

    async def more_apps(self) -> dict:
        return await self.call("/android/v1/banner/getMyBanners", {"p_name": PKG}, host="more")

    async def coin_data(self) -> dict:
        return await self.call("/android/v1/config/coin_data", {"v": VERSION_CODE, "cc": "en_US"})

    async def spinner_data(self) -> dict:
        return await self.call("/android/v1/config/spinner_data", {"v": VERSION_CODE, "cc": "en_US"})

    async def version_update(self) -> dict:
        return await self.call("/android/v1/config/vu", {"v": VERSION_CODE, "cc": "en_US"})

    async def notifications(self, date: str) -> dict:
        return await self.call("/android/v1/notification/getNotification",
                               {"v": VERSION_CODE, "date": date, "": "",
                                "DateAndTimeFirstTimeLaunch": ""})

    async def ai_effects(self) -> dict:
        return await self.call("/android/v1/effects/getExplore", {"pn": PKG}, host="ai")

    async def gen_image(self, effect_id: str, image: bytes, *, width: int, height: int,
                        ratio: str = "1:1", premium: bool = True,
                        file_name: str = "image.jpg") -> dict:
        payload = {
            "img_ratio": ratio, "effect_id": effect_id, "width": width, "height": height,
            "v": VERSION_CODE, "PACKAGE_NAME": PKG, "pn": PKG, "is_riksha": premium,
        }
        return await self.call_ai_multipart("/android/v1/loadBalance/gen_i", payload, image, file_name)

    async def upscale(self, effect_id: str, image: bytes, *, width: int, height: int,
                      ratio: str = "1:1", premium: bool = True,
                      file_name: str = "image.jpg") -> dict:
        payload = {
            "img_ratio": ratio, "effect_id": effect_id, "width": width, "height": height,
            "v": VERSION_CODE, "PACKAGE_NAME": PKG, "pn": PKG, "is_riksha": premium,
        }
        return await self.call_ai_multipart("/android/v1/loadBalance/upScale_n", payload, image, file_name)

    async def job_status(self, job_id: str, ratio: str = "1:1") -> Job:
        raw = await self.call("/android/v1/processStatus/getProcessStatus",
                              {"process_id": job_id, "cropRadio": ratio, "pn": PKG}, host="ai")
        state = str(raw.get("state", "unknown"))
        return Job(job_id=job_id, state=state,
                   done=bool(raw.get("status")), url=str(raw.get("url") or ""))

    async def cancel_job(self, job_id: str) -> dict:
        return await self.call("/android/v1/processStatus/cancelProcess",
                               {"process_id": job_id, "pn": PKG}, host="ai")
