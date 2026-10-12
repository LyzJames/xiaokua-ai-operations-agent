"""Local secrets are consumed at runtime and never exposed in API responses."""
import os
import re
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("XIAOKUA_DATA_DIR", str(ROOT / "data")))
KEY_FILE = Path(os.getenv("XIAOKUA_KEY_FILE", str(ROOT / "Key.txt")))

def credentials() -> dict[str, str]:
    aliases = {
        "deepseekapikey": "DEEPSEEK_API_KEY", "飞书appid": "FEISHU_APP_ID",
        "飞书appsecret": "FEISHU_APP_SECRET", "shopifyclientid": "SHOPIFY_CLIENT_ID",
        "shopifyclientsecret": "SHOPIFY_CLIENT_SECRET", "shopify店铺域名": "SHOPIFY_SHOP_DOMAIN",
        "shopifyshopdomain": "SHOPIFY_SHOP_DOMAIN", "shopifyaccesstoken": "SHOPIFY_ACCESS_TOKEN",
        "feishuverificationtoken": "FEISHU_VERIFICATION_TOKEN", "feishuencryptkey": "FEISHU_ENCRYPT_KEY",
        "亚马逊应用程序id": "AMAZON_APPLICATION_ID", "amazonapplicationid": "AMAZON_APPLICATION_ID",
        "亚马逊客户端编码": "AMAZON_LWA_CLIENT_ID", "amazonlwaclientid": "AMAZON_LWA_CLIENT_ID",
        "亚马逊客户端密钥": "AMAZON_LWA_CLIENT_SECRET", "amazonlwaclientsecret": "AMAZON_LWA_CLIENT_SECRET",
        "亚马逊沙盒令牌": "AMAZON_REFRESH_TOKEN", "亚马逊沙箱令牌": "AMAZON_REFRESH_TOKEN",
        "amazonrefreshtoken": "AMAZON_REFRESH_TOKEN", "amazonsellerid": "AMAZON_SELLER_ID",
    }
    values: dict[str, str] = {}
    if KEY_FILE.is_file():
        for line in KEY_FILE.read_text(encoding="utf-8-sig").splitlines():
            parts = re.split(r"\s*[:=：]\s*", line.strip(), maxsplit=1)
            if len(parts) == 2:
                field = re.sub(r"[\s_\-]", "", parts[0]).lower()
                key = aliases.get(field)
                if key:
                    values[key] = parts[1].strip().strip('"\'')
    for key in set(aliases.values()):
        if os.getenv(key):
            values[key] = os.environ[key]
        elif os.getenv(key + "_FILE"):
            values[key] = Path(os.environ[key + "_FILE"]).read_text(encoding="utf-8").strip()
    return values

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
MODEL_URL = "https://api.deepseek.com"

