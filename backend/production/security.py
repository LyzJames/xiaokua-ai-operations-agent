import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from cryptography.fernet import Fernet, MultiFernet
from .database import canonical
from ..config import ROOT

passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
ROLES = {"owner":4,"approver":3,"operator":2,"viewer":1}
def digest(v): return hashlib.sha256(v.encode()).hexdigest()
def password_hash(v):
    if len(v) < 12 or len(v) > 128: raise ValueError("密码长度须为 12–128 位")
    return passwords.hash(v)
def password_ok(encoded, value):
    try: return passwords.verify(encoded, value)
    except Argon2Error: return False

@dataclass
class Settings:
    directory: Path
    database_url: str
    public_url: str = "http://127.0.0.1:8000"
    production: bool = False
    encryption_keys: str = ""
    allow_registration: bool = False
    worker_enabled: bool = True
    test_only: bool = True

    @classmethod
    def load(cls):
        directory = Path(os.getenv("XIAOKUA_DATA_DIR", str(ROOT/"data")))
        directory.mkdir(parents=True, exist_ok=True)
        def setting(key,default=""):
            return Path(os.environ[key+"_FILE"]).read_text().strip() if os.getenv(key+"_FILE") else os.getenv(key,default)
        return cls(directory, setting("DATABASE_URL", "sqlite:///"+str(directory/"product.sqlite").replace("\\","/")),
            os.getenv("PUBLIC_URL","http://127.0.0.1:8000").rstrip("/"), os.getenv("APP_ENV") == "production",
            setting("ENCRYPTION_KEYS"), os.getenv("ALLOW_REGISTRATION") == "true", os.getenv("WORKER_ENABLED","true") == "true",
            os.getenv("SHOPIFY_TEST_ONLY","true") == "true")

    def validate(self):
        if self.production:
            if not self.public_url.startswith("https://"): raise RuntimeError("生产环境必须配置 HTTPS PUBLIC_URL")
            if not self.database_url.startswith("postgresql+psycopg://"): raise RuntimeError("生产环境必须配置 PostgreSQL")
            if not self.encryption_keys: raise RuntimeError("生产环境必须配置 ENCRYPTION_KEYS 并独立备份")
        parsed = urlparse(self.public_url)
        if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.path or parsed.query or parsed.fragment: raise RuntimeError("PUBLIC_URL 必须是无路径的固定源地址")

class Vault:
    def __init__(self, settings):
        keys = settings.encryption_keys
        if not keys:
            path = settings.directory/"encryption.key"
            if not path.exists():
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd,"wb") as f: f.write(Fernet.generate_key())
                except FileExistsError:pass
            keys = path.read_text().strip()
        self.box = MultiFernet([Fernet(k.strip().encode()) for k in keys.split(",")])
    def seal(self, obj): return self.box.encrypt(canonical(obj).encode()).decode()
    def open(self, v):
        import json
        return json.loads(self.box.decrypt(v.encode()))

def shop_domain(value):
    import re
    v = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,120}\.myshopify\.com", v): raise ValueError("请输入 *.myshopify.com 店铺域名")
    return v

def shopify_query_hmac(params, secret):
    signature = params.get("hmac", "")
    message = "&".join(f"{k}={v}" for k,v in sorted(params.items()) if k != "hmac")
    return bool(signature) and hmac.compare_digest(hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest(), signature)

def body_hmac(body, signature, secret):
    expected = base64.b64encode(hmac.new(secret.encode(),body,hashlib.sha256).digest()).decode()
    return bool(signature) and hmac.compare_digest(expected, signature)
