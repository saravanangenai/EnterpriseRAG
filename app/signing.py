"""Short-lived signed links for source images (an <img> tag cannot send a JWT header)."""
import hashlib
import hmac
import time
from urllib.parse import quote

from app.settings import get_config, get_settings


def _mac(path: str, exp: int) -> str:
    key = (get_settings().jwt_secret + "|assets").encode()
    return hmac.new(key, f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(asset_path: str) -> str:
    """asset_path is relative to the data dir, e.g. assets/<doc>/v1/p2_img0.png"""
    exp = int(time.time()) + get_config().assets.url_ttl_seconds
    return f"/{quote(asset_path)}?exp={exp}&sig={_mac(asset_path, exp)}"


def verify(asset_path: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(_mac(asset_path, exp), sig)
