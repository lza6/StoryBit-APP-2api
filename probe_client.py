"""StoryBit v3.0.6 protocol client (reverse-engineered)."""
import json, base64, ssl, urllib.request, urllib.error, uuid
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

API_URL = "https://stb.snovxgames.com:2053"
AI_URL  = "https://aa.snovxgames.com:5000"
PKG = "storybit.story.maker.animated.storymaker"
VERSION_NAME = "3.0.6"
VERSION_CODE = 140
UA = "okhttp/4.12.0"
_CTX = ssl._create_unverified_context()
IV = b"\x00" * 16

# keys published in Firebase Remote Config (project storybit-story-maker, keys ED / AI_ED / AED)
KEY_MAIN = b"ksdfjgnsjkdOLKfskjdfghJGFsrfdgad"   # RC "ED"
KEY_AI   = b"LJs97MDf0EH667sjfh6gsUsjdf897jmj"   # RC "AI_ED"
KEY_AED  = b"KJhdjkfb734OjdlKHKjJhdkgjlaj3480"   # RC "AED"

def _enc(obj, key):
    c = AES.new(key, AES.MODE_CBC, IV)
    return base64.b64encode(c.encrypt(pad(json.dumps(obj, separators=(",", ":")).encode(), 16))).decode()

def _dec(s, key):
    c = AES.new(key, AES.MODE_CBC, IV)
    return json.loads(unpad(c.decrypt(base64.b64decode(s)), 16).decode())

def _qs(cc="en_US", installer="com.android.vending"):
    return (f"?cc={cc}&version_name={VERSION_NAME}&version_code={VERSION_CODE}"
            f"&andorid_os_version=35&package_name={PKG}&installer={installer}")

def call(path, payload=None, base=API_URL, key=KEY_MAIN, timeout=30, cc="en_US", raw_part=False):
    if not path.startswith("/"):
        path = "/" + path
    url = base + path + _qs(cc)
    body = json.dumps({"data": _enc(payload if payload is not None else {}, key)}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        j = json.loads(r.read())
    if isinstance(j, dict) and isinstance(j.get("data"), str):
        try:
            return _dec(j["data"], key)
        except Exception:
            return j
    return j

def call_ai(path, payload, file_bytes=None, file_name="image.jpg", timeout=180):
    """multipart/form-data: text part 'data' (AES with AI key) + raw file part."""
    boundary = "----okhttp" + uuid.uuid4().hex
    enc = _enc(payload, KEY_AI)
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="data"\r\n'
                 f'Content-Type: text/plain; charset=UTF-8\r\n\r\n{enc}\r\n'.encode())
    if file_bytes is not None:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_name}"; '
                     f'filename="{file_name}"\r\nContent-Type: multipart/form-data\r\n\r\n'.encode())
        parts.append(file_bytes)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    if not path.startswith("/"):
        path = "/" + path
    req = urllib.request.Request(AI_URL + path, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": UA,
        "cc": "en_US", "version_name": VERSION_NAME, "version_code": str(VERSION_CODE),
        "andorid_os_version": "35", "package_name": PKG, "platform": "Android",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        j = json.loads(r.read())
    if isinstance(j, dict) and isinstance(j.get("data"), str):
        try:
            return _dec(j["data"], KEY_AI)
        except Exception:
            return j
    return j
