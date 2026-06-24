from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class IntegrationConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class JushuitanConfig:
    app_key: str
    app_secret: str
    access_token: str
    shop_id: str
    auth_url: str
    redirect_uri: str
    product_endpoint: str
    inventory_endpoint: str

    @classmethod
    def from_env(cls) -> "JushuitanConfig":
        return cls(
            app_key=os.getenv("JST_APP_KEY", ""),
            app_secret=os.getenv("JST_APP_SECRET", ""),
            access_token=os.getenv("JST_ACCESS_TOKEN", ""),
            shop_id=os.getenv("JST_SHOP_ID", ""),
            auth_url=os.getenv("JST_AUTH_URL", ""),
            redirect_uri=os.getenv("JST_REDIRECT_URI", ""),
            product_endpoint=os.getenv("JST_PRODUCT_ENDPOINT", ""),
            inventory_endpoint=os.getenv("JST_INVENTORY_ENDPOINT", ""),
        )

    def missing_for_sync(self) -> list[str]:
        required = {
            "JST_APP_KEY": self.app_key,
            "JST_APP_SECRET": self.app_secret,
            "JST_ACCESS_TOKEN": self.access_token,
            "JST_INVENTORY_ENDPOINT": self.inventory_endpoint,
        }
        return [name for name, value in required.items() if not value]

    def missing_for_authorize(self) -> list[str]:
        required = {
            "JST_APP_KEY": self.app_key,
            "JST_AUTH_URL": self.auth_url,
            "JST_REDIRECT_URI": self.redirect_uri,
        }
        return [name for name, value in required.items() if not value]


def mask_secret(value: str) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:4]}***{value[-4:]}"


def build_authorize_url(config: JushuitanConfig, tenant_id: str) -> str:
    missing = config.missing_for_authorize()
    if missing:
        raise IntegrationConfigError(f"缺少聚水潭授权配置：{', '.join(missing)}")
    separator = "&" if "?" in config.auth_url else "?"
    return (
        f"{config.auth_url}{separator}"
        f"app_key={config.app_key}&redirect_uri={config.redirect_uri}&state={tenant_id}"
    )


class OfficialJushuitanAdapter:
    mode = "official"

    def __init__(self, config: JushuitanConfig | None = None) -> None:
        self.config = config or JushuitanConfig.from_env()

    def fetch_inventory(self, tenant_id: str) -> list[dict[str, Any]]:
        missing = self.config.missing_for_sync()
        if missing:
            raise IntegrationConfigError(f"无法同步聚水潭真实数据，缺少配置：{', '.join(missing)}")

        payload = {
            "tenant_id": tenant_id,
            "shop_id": self.config.shop_id,
            "page_index": 1,
            "page_size": 100,
        }
        data = self._post_json(self.config.inventory_endpoint, payload)
        records = data.get("data") or data.get("items") or data.get("list") or []
        if isinstance(records, dict):
            records = records.get("items") or records.get("list") or []
        if not isinstance(records, list):
            raise RuntimeError("聚水潭库存接口返回格式无法识别：未找到列表数据")
        return [self._normalize_inventory_item(item) for item in records]

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.access_token}",
                "X-App-Key": self.config.app_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"聚水潭接口 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"聚水潭接口连接失败：{exc.reason}") from exc

        data = json.loads(body)
        if isinstance(data, dict) and data.get("code") not in (None, 0, "0", 200, "200"):
            raise RuntimeError(f"聚水潭接口返回失败：{data.get('msg') or data.get('message') or data}")
        return data

    def _normalize_inventory_item(self, item: dict[str, Any]) -> dict[str, Any]:
        sku = item.get("sku") or item.get("sku_id") or item.get("sku_code") or item.get("i_id")
        if not sku:
            raise RuntimeError(f"聚水潭库存记录缺少 SKU：{item}")
        stock = item.get("stock") or item.get("qty") or item.get("available_qty") or 0
        cost = item.get("cost") or item.get("cost_price") or item.get("purchase_price") or 0
        return {"sku": str(sku), "stock": int(float(stock)), "cost": float(cost)}

