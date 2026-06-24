from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
import csv
import hashlib
import io
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from backend.jushuitan import (
    IntegrationConfigError,
    JushuitanConfig,
    OfficialJushuitanAdapter,
    build_authorize_url,
    mask_secret,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "commerce.db"))
INTEGRATION_MODE = os.getenv("INTEGRATION_MODE", "mock")
DEFAULT_TENANT = "tenant-qingyang"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_competitor_columns(db: sqlite3.Connection) -> None:
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='competitor_snapshots'").fetchone():
        return
    cols = {row[1] for row in db.execute("PRAGMA table_info(competitor_snapshots)").fetchall()}
    for name in ["image_url", "time_range", "category_path", "carrier", "brand_filter"]:
        if name not in cols:
            db.execute(f"ALTER TABLE competitor_snapshots ADD COLUMN {name} TEXT")


def ensure_crm_columns(db: sqlite3.Connection) -> None:
    if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='influencers'").fetchone():
        return
    cols = {row[1] for row in db.execute("PRAGMA table_info(influencers)").fetchall()}
    defaults = {
        "profile_url": "TEXT",
        "next_follow_at": "TEXT",
        "notes": "TEXT",
        "followers": "INTEGER NOT NULL DEFAULT 0",
        "quote_price": "REAL NOT NULL DEFAULT 0",
        "contact": "TEXT",
        "owner_id": "TEXT",
    }
    for name, spec in defaults.items():
        if name not in cols:
            db.execute(f"ALTER TABLE influencers ADD COLUMN {name} {spec}")


def initialize_database() -> None:
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), name TEXT NOT NULL,
          sku TEXT NOT NULL, stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
          cost REAL NOT NULL DEFAULT 0 CHECK(cost >= 0), price REAL NOT NULL DEFAULT 0 CHECK(price >= 0),
          status TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(tenant_id, sku));
        CREATE TABLE IF NOT EXISTS sync_jobs (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), provider TEXT NOT NULL,
          mode TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
          records_seen INTEGER NOT NULL DEFAULT 0, records_updated INTEGER NOT NULL DEFAULT 0,
          error TEXT, started_at TEXT NOT NULL, finished_at TEXT, UNIQUE(tenant_id, provider, idempotency_key));
        CREATE TABLE IF NOT EXISTS audit_logs (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), actor TEXT NOT NULL,
          action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publish_jobs (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), product_id TEXT NOT NULL REFERENCES products(id),
          platform TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL, external_id TEXT, created_at TEXT NOT NULL,
          UNIQUE(tenant_id, product_id, platform, status));
        CREATE TABLE IF NOT EXISTS external_authorizations (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), provider TEXT NOT NULL,
          status TEXT NOT NULL, app_key_masked TEXT, shop_id TEXT, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(tenant_id, provider));
        CREATE TABLE IF NOT EXISTS competitor_snapshots (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), platform TEXT NOT NULL, source_url TEXT NOT NULL,
          ranking_name TEXT NOT NULL, time_range TEXT, category_path TEXT, carrier TEXT, brand_filter TEXT, rank_no INTEGER NOT NULL, product_name TEXT NOT NULL, deal_price REAL, image_url TEXT,
          shop_name TEXT, shop_id TEXT, transaction_amount_range TEXT, click_count_range TEXT, growth_rate_range TEXT,
          conversion_rate_range TEXT, raw_payload TEXT NOT NULL, captured_at TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        db.execute("INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)", (DEFAULT_TENANT, "轻养生活旗舰店", utc_now()))
        seed = [
            ("p-001", "益生菌冻干粉 30袋", "QY-YSD-30", 862, 38.6, 89.0, "在库"),
            ("p-002", "叶黄素酯软糖 60粒", "QY-YHS-60", 126, 24.8, 69.0, "低库存"),
            ("p-003", "胶原蛋白肽饮 10瓶", "QY-JYD-10", 430, 51.2, 119.0, "在库"),
            ("p-004", "维生素C咀嚼片 90片", "QY-VCC-90", 706, 16.5, 49.0, "在库"),
            ("p-005", "鱼油软胶囊 60粒", "QY-YYR-60", 0, 33.4, 79.0, "缺货"),
        ]
        ensure_crm_columns(db)
        for item in seed:
            db.execute("""INSERT OR IGNORE INTO products
              (id,tenant_id,name,sku,stock,cost,price,status,source,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (item[0], DEFAULT_TENANT, *item[1:], "seed", utc_now()))

        db.executescript("""
        CREATE TABLE IF NOT EXISTS crm_users (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), username TEXT NOT NULL,
          password_hash TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(tenant_id, username));
        CREATE TABLE IF NOT EXISTS crm_auth_sessions (
          token TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), user_id TEXT NOT NULL REFERENCES crm_users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS influencers (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), platform TEXT NOT NULL,
          account TEXT NOT NULL, nickname TEXT, profile_url TEXT, category TEXT, followers INTEGER NOT NULL DEFAULT 0,
          quote_price REAL NOT NULL DEFAULT 0, contact TEXT, owner_id TEXT REFERENCES crm_users(id), status TEXT NOT NULL,
          next_follow_at TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(tenant_id, platform, account));
        CREATE TABLE IF NOT EXISTS influencer_followups (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), influencer_id TEXT NOT NULL REFERENCES influencers(id),
          user_id TEXT REFERENCES crm_users(id), action TEXT NOT NULL, note TEXT, next_follow_at TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS influencer_samples (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), influencer_id TEXT NOT NULL REFERENCES influencers(id),
          sample_date TEXT NOT NULL, sample_name TEXT NOT NULL, tracking_no TEXT, receive_status TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS influencer_deals (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), influencer_id TEXT NOT NULL REFERENCES influencers(id),
          deal_date TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, cooperation_type TEXT, notes TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS influencer_status_logs (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), influencer_id TEXT NOT NULL REFERENCES influencers(id),
          from_status TEXT, to_status TEXT NOT NULL, user_id TEXT REFERENCES crm_users(id), created_at TEXT NOT NULL);
        """)
        crm_users = [
            ("u-admin", "admin", "admin123", "管理员", "admin"),
            ("u-manager", "manager", "manager123", "运营主管", "manager"),
            ("u-bd", "bd", "bd123", "一线BD", "bd"),
        ]
        for user_id, username, password, display_name, user_role in crm_users:
            db.execute(
                """INSERT OR IGNORE INTO crm_users(id,tenant_id,username,password_hash,display_name,role,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (user_id, DEFAULT_TENANT, username, hashlib.sha256(password.encode("utf-8")).hexdigest(), display_name, user_role, utc_now()),
            )
        sample_influencers = [
            ("inf-001", "快手", "yangsheng-lili", "养生丽丽", "养生茶饮", 186000, 3200, "wx:lili2026", "u-bd", "已建联", "2026-06-24", "适合冻干粉试用种草"),
            ("inf-002", "抖音", "family-health", "家庭健康官", "营养补充", 98000, 1800, "wx:health98", "u-bd", "已建联", "2026-06-25", "已沟通软糖合作"),
            ("inf-003", "小红书", "mama-light", "轻养妈妈", "家庭健康", 126000, 2600, "wx:mama_light", "u-manager", "寄样", "2026-06-23", "等待收样反馈"),
            ("inf-004", "快手", "tea-daily", "每日一杯茶", "养生茶饮", 214000, 4200, "wx:teadaily", "u-bd", "已出单", "", "首单合作已成交"),
        ]
        for influencer in sample_influencers:
            now = utc_now()
            db.execute(
                """INSERT OR IGNORE INTO influencers(id,tenant_id,platform,account,nickname,category,followers,quote_price,contact,owner_id,status,next_follow_at,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (influencer[0], DEFAULT_TENANT, influencer[1], influencer[2], influencer[3], influencer[4], influencer[5], influencer[6], influencer[7], influencer[8], influencer[9], influencer[10], influencer[11], now, now),
            )


class RequestContext(BaseModel):
    tenant_id: str
    actor: str
    role: Literal["super_admin", "operator", "admin", "manager", "bd"]
    user_id: str | None = None


def request_context(
    tenant_id: Annotated[str, Header(alias="X-Tenant-ID")] = DEFAULT_TENANT,
    actor: Annotated[str, Header(alias="X-Actor")] = "demo-operator",
    role: Annotated[str, Header(alias="X-User-Role")] = "operator",
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> RequestContext:
    with database() as db:
        exists = db.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Tenant not found")
        if authorization and authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
            session = db.execute(
                """SELECT s.user_id,u.username,u.role,u.tenant_id FROM crm_auth_sessions s
                   JOIN crm_users u ON u.id=s.user_id WHERE s.token=?""",
                (token,),
            ).fetchone()
            if not session:
                raise HTTPException(401, "Invalid session")
            return RequestContext(tenant_id=session["tenant_id"], actor=session["username"], role=session["role"], user_id=session["user_id"])
    if role not in {"super_admin", "operator", "admin", "manager", "bd"}:
        raise HTTPException(403, "Unsupported role")
    return RequestContext(tenant_id=tenant_id, actor=actor, role=role)


class ProductOut(BaseModel):
    id: str
    name: str
    sku: str
    stock: int
    cost: float
    price: float
    status: str
    source: str
    updated_at: str


class SyncRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


class SyncJobOut(BaseModel):
    id: str
    provider: str
    mode: str
    status: str
    records_seen: int
    records_updated: int
    error: str | None
    started_at: str
    finished_at: str | None


class PublishRequest(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=100)


class MockJushuitanAdapter:
    mode = "mock"

    def fetch_inventory(self, tenant_id: str) -> list[dict]:
        with database() as db:
            rows = db.execute("SELECT sku,stock,cost FROM products WHERE tenant_id=? ORDER BY sku", (tenant_id,)).fetchall()
        return [dict(row) for row in rows]


class MockKuaishouAdapter:
    mode = "mock"

    def create_draft(self, product: sqlite3.Row) -> str:
        return f"KS-MOCK-{product['sku']}-{uuid.uuid4().hex[:6].upper()}"


def get_jushuitan_adapter():
    if INTEGRATION_MODE.lower() in {"official", "production", "prod"}:
        return OfficialJushuitanAdapter()
    return MockJushuitanAdapter()


def audit(db: sqlite3.Connection, context: RequestContext, action: str, target_type: str, target_id: str | None, payload: dict) -> None:
    db.execute(
        "INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), context.tenant_id, context.actor, action, target_type, target_id, json.dumps(payload, ensure_ascii=False), utc_now()),
    )


def execute_sync(job_id: str, context: RequestContext) -> None:
    adapter = get_jushuitan_adapter()
    try:
        snapshot = adapter.fetch_inventory(context.tenant_id)
        updated = 0
        with database() as db:
            for item in snapshot:
                status = "缺货" if item["stock"] == 0 else ("低库存" if item["stock"] < 150 else "在库")
                cursor = db.execute(
                    """UPDATE products SET stock=?,cost=?,status=?,source=?,updated_at=?
                    WHERE tenant_id=? AND sku=?""",
                    (item["stock"], item["cost"], status, adapter.mode, utc_now(), context.tenant_id, item["sku"]),
                )
                updated += cursor.rowcount
            db.execute(
                "UPDATE sync_jobs SET status='completed',records_seen=?,records_updated=?,finished_at=? WHERE id=? AND tenant_id=?",
                (len(snapshot), updated, utc_now(), job_id, context.tenant_id),
            )
            audit(db, context, "inventory.sync.completed", "sync_job", job_id, {"seen": len(snapshot), "updated": updated, "mode": adapter.mode})
    except IntegrationConfigError as exc:
        with database() as db:
            db.execute(
                "UPDATE sync_jobs SET status='failed',error=?,finished_at=? WHERE id=? AND tenant_id=?",
                (str(exc)[:500], utc_now(), job_id, context.tenant_id),
            )
            db.execute(
                """INSERT INTO external_authorizations(id,tenant_id,provider,status,last_error,created_at,updated_at)
                   VALUES(?,?,'jushuitan','config_missing',?,?,?)
                   ON CONFLICT(tenant_id,provider) DO UPDATE SET status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), context.tenant_id, str(exc)[:500], utc_now(), utc_now()),
            )
            audit(db, context, "inventory.sync.config_missing", "sync_job", job_id, {"error": str(exc), "mode": adapter.mode})
    except Exception as exc:
        with database() as db:
            db.execute(
                "UPDATE sync_jobs SET status='failed',error=?,finished_at=? WHERE id=? AND tenant_id=?",
                (str(exc)[:500], utc_now(), job_id, context.tenant_id),
            )
            audit(db, context, "inventory.sync.failed", "sync_job", job_id, {"error": str(exc), "mode": adapter.mode})


app = FastAPI(title="快营智擎 API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000", "https://deploy-static-rose.vercel.app"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "integration_mode": INTEGRATION_MODE, "database": DB_PATH.name}


@app.get("/api/integrations/jushuitan/status")
def jushuitan_status(context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    config = JushuitanConfig.from_env()
    sync_missing = config.missing_for_sync()
    auth_missing = config.missing_for_authorize()
    with database() as db:
        saved = db.execute(
            "SELECT status,app_key_masked,shop_id,last_error,updated_at FROM external_authorizations WHERE tenant_id=? AND provider='jushuitan'",
            (context.tenant_id,),
        ).fetchone()
    return {
        "provider": "jushuitan",
        "mode": INTEGRATION_MODE,
        "ready_for_authorize": not auth_missing,
        "ready_for_sync": not sync_missing,
        "missing_for_authorize": auth_missing,
        "missing_for_sync": sync_missing,
        "app_key_masked": mask_secret(config.app_key),
        "shop_id": config.shop_id or None,
        "saved_authorization": dict(saved) if saved else None,
    }


@app.get("/api/integrations/jushuitan/authorize-url")
def jushuitan_authorize_url(context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    config = JushuitanConfig.from_env()
    try:
        authorize_url = build_authorize_url(config, context.tenant_id)
    except IntegrationConfigError as exc:
        raise HTTPException(424, str(exc)) from exc
    with database() as db:
        db.execute(
            """INSERT INTO external_authorizations(id,tenant_id,provider,status,app_key_masked,shop_id,created_at,updated_at)
               VALUES(?,?,'jushuitan','authorize_url_created',?,?,?,?)
               ON CONFLICT(tenant_id,provider) DO UPDATE SET status=excluded.status,app_key_masked=excluded.app_key_masked,
               shop_id=excluded.shop_id,updated_at=excluded.updated_at,last_error=NULL""",
            (str(uuid.uuid4()), context.tenant_id, mask_secret(config.app_key), config.shop_id, utc_now(), utc_now()),
        )
        audit(db, context, "jushuitan.authorize_url.created", "external_authorization", None, {"shop_id": config.shop_id})
    return {"authorize_url": authorize_url, "state": context.tenant_id}


@app.get("/api/products", response_model=list[ProductOut])
def list_products(context: Annotated[RequestContext, Depends(request_context)], search: str = Query("", max_length=100)) -> list[dict]:
    with database() as db:
        rows = db.execute(
            """SELECT id,name,sku,stock,cost,price,status,source,updated_at FROM products
          WHERE tenant_id=? AND (name LIKE ? OR sku LIKE ?) ORDER BY updated_at DESC""",
            (context.tenant_id, f"%{search}%", f"%{search}%"),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/dashboard")
def dashboard(context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    with database() as db:
        summary = db.execute(
            "SELECT COUNT(*) product_count,SUM(stock) total_stock,ROUND(SUM(MAX(price-cost,0)*stock),2) potential_margin FROM products WHERE tenant_id=?",
            (context.tenant_id,),
        ).fetchone()
        last_sync = db.execute("SELECT * FROM sync_jobs WHERE tenant_id=? ORDER BY started_at DESC LIMIT 1", (context.tenant_id,)).fetchone()
    return {"summary": dict(summary), "last_sync": dict(last_sync) if last_sync else None, "data_mode": INTEGRATION_MODE}


@app.post("/api/sync/jushuitan", response_model=SyncJobOut, status_code=202)
def sync_jushuitan(body: SyncRequest, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    with database() as db:
        existing = db.execute(
            "SELECT * FROM sync_jobs WHERE tenant_id=? AND provider='jushuitan' AND idempotency_key=?",
            (context.tenant_id, body.idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing)
        job_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO sync_jobs(id,tenant_id,provider,mode,status,idempotency_key,started_at)
          VALUES(?,?,'jushuitan',?,'running',?,?)""",
            (job_id, context.tenant_id, INTEGRATION_MODE, body.idempotency_key, utc_now()),
        )
        audit(db, context, "inventory.sync.started", "sync_job", job_id, {"mode": INTEGRATION_MODE})
    threading.Thread(target=execute_sync, args=(job_id, context), daemon=True).start()
    with database() as db:
        return dict(db.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone())


@app.get("/api/sync/jobs/{job_id}", response_model=SyncJobOut)
def get_sync_job(job_id: str, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    with database() as db:
        row = db.execute("SELECT * FROM sync_jobs WHERE id=? AND tenant_id=?", (job_id, context.tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "Sync job not found")
    return dict(row)


@app.post("/api/kuaishou/drafts", status_code=202)
def create_drafts(body: PublishRequest, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    adapter, created = MockKuaishouAdapter(), []
    with database() as db:
        for product_id in body.product_ids:
            product = db.execute("SELECT * FROM products WHERE id=? AND tenant_id=?", (product_id, context.tenant_id)).fetchone()
            if not product:
                raise HTTPException(404, f"Product {product_id} not found")
            external_id, job_id = adapter.create_draft(product), str(uuid.uuid4())
            db.execute(
                """INSERT OR IGNORE INTO publish_jobs(id,tenant_id,product_id,platform,status,mode,external_id,created_at)
              VALUES(?,?,?,'kuaishou','draft_created',?,?,?)""",
                (job_id, context.tenant_id, product_id, adapter.mode, external_id, utc_now()),
            )
            created.append({"product_id": product_id, "external_id": external_id, "mode": adapter.mode})
        audit(db, context, "kuaishou.drafts.created", "publish_job", None, {"count": len(created), "mode": adapter.mode})
    return {"created": created, "mode": adapter.mode, "production_synced": False}


@app.get("/api/competitor-snapshots")
def list_competitor_snapshots(
    context: Annotated[RequestContext, Depends(request_context)],
    limit: int = Query(200, ge=1, le=500),
    captured_date: str = Query("", max_length=20),
    time_range: str = Query("", max_length=50),
    category_path: str = Query("", max_length=200),
    carrier: str = Query("", max_length=50),
    brand_filter: str = Query("", max_length=50),
) -> dict:
    clauses = ["tenant_id=?", "platform='kuaishou'"]
    params: list = [context.tenant_id]
    if captured_date:
        clauses.append("substr(captured_at,1,10)=?")
        params.append(captured_date)
    if time_range:
        clauses.append("COALESCE(time_range,'')=?")
        params.append(time_range)
    if category_path:
        clauses.append("COALESCE(category_path,'')=?")
        params.append(category_path)
    if carrier:
        clauses.append("COALESCE(carrier,'')=?")
        params.append(carrier)
    if brand_filter:
        clauses.append("COALESCE(brand_filter,'')=?")
        params.append(brand_filter)
    where = " AND ".join(clauses)
    with database() as db:
        ensure_competitor_columns(db)
        rows = db.execute(
            f"""SELECT rank_no,product_name,deal_price,image_url,shop_name,shop_id,transaction_amount_range,click_count_range,
                      growth_rate_range,conversion_rate_range,captured_at,source_url,time_range,category_path,carrier,brand_filter
               FROM competitor_snapshots WHERE {where}
               ORDER BY captured_at DESC, rank_no ASC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        filters = {
            "dates": [r[0] for r in db.execute("SELECT DISTINCT substr(captured_at,1,10) FROM competitor_snapshots WHERE tenant_id=? AND platform='kuaishou' ORDER BY 1 DESC", (context.tenant_id,)).fetchall() if r[0]],
            "time_ranges": [r[0] for r in db.execute("SELECT DISTINCT COALESCE(time_range,'') FROM competitor_snapshots WHERE tenant_id=? AND platform='kuaishou' ORDER BY 1", (context.tenant_id,)).fetchall() if r[0]],
            "category_paths": [r[0] for r in db.execute("SELECT DISTINCT COALESCE(category_path,'') FROM competitor_snapshots WHERE tenant_id=? AND platform='kuaishou' ORDER BY 1", (context.tenant_id,)).fetchall() if r[0]],
            "carriers": [r[0] for r in db.execute("SELECT DISTINCT COALESCE(carrier,'') FROM competitor_snapshots WHERE tenant_id=? AND platform='kuaishou' ORDER BY 1", (context.tenant_id,)).fetchall() if r[0]],
            "brand_filters": [r[0] for r in db.execute("SELECT DISTINCT COALESCE(brand_filter,'') FROM competitor_snapshots WHERE tenant_id=? AND platform='kuaishou' ORDER BY 1", (context.tenant_id,)).fetchall() if r[0]],
        }
    return {"items": [dict(row) for row in rows], "filters": filters}


@app.get("/api/audit-logs")
def list_audit_logs(context: Annotated[RequestContext, Depends(request_context)], limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    with database() as db:
        rows = db.execute(
            "SELECT actor,action,target_type,target_id,payload,created_at FROM audit_logs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (context.tenant_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


CRM_STATUSES = {"未建联", "已建联", "寄样", "已出单", "定期维护"}
CRM_ACTION_TO_STATUS = {"未建联": "未建联", "已建联": "已建联", "寄样": "寄样", "已出单": "已出单", "定期维护": "定期维护", "邀约": "未建联", "建联": "已建联", "成交": "已出单", "失败": "未建联"}


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def crm_can_manage_all(context: RequestContext) -> bool:
    return context.role in {"super_admin", "admin", "manager", "operator"}


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=3, max_length=100)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class InfluencerIn(BaseModel):
    platform: str = Field(min_length=1, max_length=30)
    account: str = Field(min_length=1, max_length=100)
    nickname: str = Field("", max_length=100)
    profile_url: str = Field("", max_length=300)
    category: str = Field("", max_length=100)
    followers: str | int = "0"
    quote_price: float = Field(0, ge=0)
    contact: str = Field("", max_length=120)
    owner_id: str | None = None
    status: str = "未建联"
    next_follow_at: str = ""
    notes: str = Field("", max_length=1000)


class FollowupIn(BaseModel):
    action: str = Field(min_length=1, max_length=20)
    note: str = Field("", max_length=1000)
    next_follow_at: str = ""


class SampleIn(BaseModel):
    sample_date: str = Field(min_length=4, max_length=20)
    sample_name: str = Field(min_length=1, max_length=120)
    tracking_no: str = Field("", max_length=120)
    receive_status: str = Field("未签收", max_length=40)
    notes: str = Field("", max_length=500)


class DealIn(BaseModel):
    deal_date: str = Field(min_length=4, max_length=20)
    amount: float = Field(0, ge=0)
    cooperation_type: str = Field("", max_length=80)
    notes: str = Field("", max_length=500)


class ImportCsvRequest(BaseModel):
    csv_text: str = Field(min_length=1)


@app.post("/api/auth/login")
def crm_login(body: LoginRequest) -> dict:
    with database() as db:
        user = db.execute(
            "SELECT id,tenant_id,username,display_name,role,password_hash FROM crm_users WHERE tenant_id=? AND username=?",
            (DEFAULT_TENANT, body.username),
        ).fetchone()
        if not user or user["password_hash"] != hash_password(body.password):
            raise HTTPException(401, "用户名或密码错误")
        token = uuid.uuid4().hex + uuid.uuid4().hex
        db.execute("INSERT INTO crm_auth_sessions(token,tenant_id,user_id,created_at) VALUES(?,?,?,?)", (token, user["tenant_id"], user["id"], utc_now()))
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "role": user["role"]}}


@app.get("/api/crm/users", response_model=list[UserOut])
def crm_users(context: Annotated[RequestContext, Depends(request_context)]) -> list[dict]:
    with database() as db:
        rows = db.execute("SELECT id,username,display_name,role FROM crm_users WHERE tenant_id=? ORDER BY role,display_name", (context.tenant_id,)).fetchall()
    return [dict(row) for row in rows]


def influencer_visible_clause(context: RequestContext) -> tuple[str, list]:
    if crm_can_manage_all(context) or not context.user_id:
        return "i.tenant_id=?", [context.tenant_id]
    return "i.tenant_id=? AND i.owner_id=?", [context.tenant_id, context.user_id]


@app.get("/api/crm/influencers")
def list_influencers(
    context: Annotated[RequestContext, Depends(request_context)],
    search: str = Query("", max_length=100),
    status: str = Query("", max_length=20),
    platform: str = Query("", max_length=30),
    category: str = Query("", max_length=100),
    owner_id: str = Query("", max_length=80),
    date: str = Query("", max_length=20),
) -> dict:
    clause, params = influencer_visible_clause(context)
    clauses = [clause]
    if search:
        clauses.append("(account LIKE ? OR nickname LIKE ? OR contact LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if status:
        clauses.append("status=?")
        params.append(status)
    if platform:
        clauses.append("platform=?")
        params.append(platform)
    if category:
        clauses.append("category=?")
        params.append(category)
    if owner_id and crm_can_manage_all(context):
        clauses.append("owner_id=?")
        params.append(owner_id)
    if date:
        clauses.append("substr(created_at,1,10)=?")
        params.append(date)
    where = " AND ".join(clauses)
    with database() as db:
        rows = db.execute(
            f"""SELECT i.*,u.display_name owner_name,COALESCE(MAX(f.created_at), i.created_at) follow_time
                FROM influencers i
                LEFT JOIN crm_users u ON u.id=i.owner_id
                LEFT JOIN influencer_followups f ON f.influencer_id=i.id
                WHERE {where}
                GROUP BY i.id
                ORDER BY follow_time DESC, i.updated_at DESC""",
            params,
        ).fetchall()
    return {"items": [dict(row) for row in rows], "statuses": sorted(CRM_STATUSES)}


@app.post("/api/crm/influencers", status_code=201)
def create_influencer(body: InfluencerIn, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    status = body.status if body.status in CRM_STATUSES else "未建联"
    owner_id = body.owner_id if crm_can_manage_all(context) and body.owner_id else context.user_id
    now = utc_now()
    influencer_id = str(uuid.uuid4())
    with database() as db:
        try:
            db.execute(
                """INSERT INTO influencers(id,tenant_id,platform,account,nickname,profile_url,category,followers,quote_price,contact,owner_id,status,next_follow_at,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (influencer_id, context.tenant_id, body.platform, body.account, body.nickname, body.profile_url, body.category, body.followers, body.quote_price, body.contact, owner_id, status, body.next_follow_at, body.notes, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "同平台达人账号已存在") from exc
        db.execute("INSERT INTO influencer_status_logs VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, None, status, context.user_id, now))
        audit(db, context, "influencer.created", "influencer", influencer_id, body.model_dump())
    return {"id": influencer_id}


@app.post("/api/crm/influencers/{influencer_id}/followups", status_code=201)
def create_followup(influencer_id: str, body: FollowupIn, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    target_status = CRM_ACTION_TO_STATUS.get(body.action)
    now = utc_now()
    with database() as db:
        influencer = db.execute("SELECT * FROM influencers WHERE id=? AND tenant_id=?", (influencer_id, context.tenant_id)).fetchone()
        if not influencer:
            raise HTTPException(404, "Influencer not found")
        if not crm_can_manage_all(context) and context.user_id and influencer["owner_id"] != context.user_id:
            raise HTTPException(403, "No permission for this influencer")
        db.execute("INSERT INTO influencer_followups VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, context.user_id, body.action, body.note, body.next_follow_at, now))
        if target_status and target_status != influencer["status"]:
            db.execute("UPDATE influencers SET status=?,next_follow_at=?,updated_at=? WHERE id=?", (target_status, body.next_follow_at, now, influencer_id))
            db.execute("INSERT INTO influencer_status_logs VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, influencer["status"], target_status, context.user_id, now))
        elif body.next_follow_at:
            db.execute("UPDATE influencers SET next_follow_at=?,updated_at=? WHERE id=?", (body.next_follow_at, now, influencer_id))
        audit(db, context, "influencer.followup.created", "influencer", influencer_id, body.model_dump())
    return {"ok": True, "status": target_status or influencer["status"]}


@app.post("/api/crm/influencers/{influencer_id}/samples", status_code=201)
def create_sample(influencer_id: str, body: SampleIn, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    now = utc_now()
    with database() as db:
        influencer = db.execute("SELECT * FROM influencers WHERE id=? AND tenant_id=?", (influencer_id, context.tenant_id)).fetchone()
        if not influencer:
            raise HTTPException(404, "Influencer not found")
        db.execute("INSERT INTO influencer_samples VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, body.sample_date, body.sample_name, body.tracking_no, body.receive_status, body.notes, now))
        if influencer["status"] != "寄样":
            db.execute("UPDATE influencers SET status='寄样',updated_at=? WHERE id=?", (now, influencer_id))
            db.execute("INSERT INTO influencer_status_logs VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, influencer["status"], "寄样", context.user_id, now))
        db.execute("INSERT INTO influencer_followups VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, context.user_id, "寄样", body.notes, "", now))
        audit(db, context, "influencer.sample.created", "influencer", influencer_id, body.model_dump())
    return {"ok": True}


@app.post("/api/crm/influencers/{influencer_id}/deals", status_code=201)
def create_deal(influencer_id: str, body: DealIn, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    now = utc_now()
    with database() as db:
        influencer = db.execute("SELECT * FROM influencers WHERE id=? AND tenant_id=?", (influencer_id, context.tenant_id)).fetchone()
        if not influencer:
            raise HTTPException(404, "Influencer not found")
        db.execute("INSERT INTO influencer_deals VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, body.deal_date, body.amount, body.cooperation_type, body.notes, now))
        if influencer["status"] != "已出单":
            db.execute("UPDATE influencers SET status='已出单',updated_at=? WHERE id=?", (now, influencer_id))
            db.execute("INSERT INTO influencer_status_logs VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, influencer["status"], "已出单", context.user_id, now))
        db.execute("INSERT INTO influencer_followups VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, context.user_id, "成交", body.notes, "", now))
        audit(db, context, "influencer.deal.created", "influencer", influencer_id, body.model_dump())
    return {"ok": True}


@app.get("/api/crm/influencers/{influencer_id}")
def influencer_detail(influencer_id: str, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    with database() as db:
        influencer = db.execute("SELECT i.*,u.display_name owner_name FROM influencers i LEFT JOIN crm_users u ON u.id=i.owner_id WHERE i.id=? AND i.tenant_id=?", (influencer_id, context.tenant_id)).fetchone()
        if not influencer:
            raise HTTPException(404, "Influencer not found")
        followups = db.execute("SELECT f.*,u.display_name user_name FROM influencer_followups f LEFT JOIN crm_users u ON u.id=f.user_id WHERE f.influencer_id=? ORDER BY f.created_at DESC", (influencer_id,)).fetchall()
        samples = db.execute("SELECT * FROM influencer_samples WHERE influencer_id=? ORDER BY sample_date DESC", (influencer_id,)).fetchall()
        deals = db.execute("SELECT * FROM influencer_deals WHERE influencer_id=? ORDER BY deal_date DESC", (influencer_id,)).fetchall()
    return {"influencer": dict(influencer), "followups": [dict(r) for r in followups], "samples": [dict(r) for r in samples], "deals": [dict(r) for r in deals]}


@app.get("/api/crm/dashboard")
def crm_dashboard(context: Annotated[RequestContext, Depends(request_context)], date: str = Query("", max_length=20)) -> dict:
    day = date or datetime.now(timezone.utc).date().isoformat()
    owner_clause = ""
    params = [context.tenant_id, day]
    if not crm_can_manage_all(context) and context.user_id:
        owner_clause = " AND i.owner_id=?"
        params.append(context.user_id)
    with database() as db:
        status_counts = {row["status"]: row["count"] for row in db.execute("SELECT status,COUNT(*) count FROM influencers WHERE tenant_id=? GROUP BY status", (context.tenant_id,)).fetchall()}
        def log_count(status: str) -> int:
            return db.execute(
                f"""SELECT COUNT(DISTINCT l.influencer_id) count FROM influencer_status_logs l
                    JOIN influencers i ON i.id=l.influencer_id WHERE l.tenant_id=? AND substr(l.created_at,1,10)=? AND l.to_status=?{owner_clause}""",
                (*params, status),
            ).fetchone()["count"]
        invited = log_count("已建联")
        connected = log_count("已建联")
        samples = db.execute(
            f"""SELECT COUNT(DISTINCT s.influencer_id) count FROM influencer_samples s JOIN influencers i ON i.id=s.influencer_id
                WHERE s.tenant_id=? AND substr(s.sample_date,1,10)=?{owner_clause}""",
            params,
        ).fetchone()["count"]
        deals = db.execute(
            f"""SELECT COUNT(DISTINCT d.influencer_id) count,COALESCE(SUM(d.amount),0) amount FROM influencer_deals d JOIN influencers i ON i.id=d.influencer_id
                WHERE d.tenant_id=? AND substr(d.deal_date,1,10)=?{owner_clause}""",
            params,
        ).fetchone()
        new_accounts = db.execute(f"SELECT COUNT(*) count FROM influencers i WHERE i.tenant_id=? AND substr(i.created_at,1,10)=?{owner_clause}", params).fetchone()["count"]
        overdue = db.execute("SELECT COUNT(*) count FROM influencers WHERE tenant_id=? AND next_follow_at<>'' AND next_follow_at<? AND status NOT IN ('已出单','未建联')", (context.tenant_id, day)).fetchone()["count"]
        owner_rows = db.execute(
            """SELECT u.display_name owner_name,
                      COUNT(i.id) total,
                      SUM(CASE WHEN i.status='已建联' THEN 1 ELSE 0 END) invited,
                      SUM(CASE WHEN i.status='已建联' THEN 1 ELSE 0 END) connected,
                      SUM(CASE WHEN i.status='寄样' THEN 1 ELSE 0 END) sampled,
                      SUM(CASE WHEN i.status='已出单' THEN 1 ELSE 0 END) dealed
               FROM crm_users u LEFT JOIN influencers i ON i.owner_id=u.id AND i.tenant_id=u.tenant_id
               WHERE u.tenant_id=? GROUP BY u.id ORDER BY dealed DESC,total DESC""",
            (context.tenant_id,),
        ).fetchall()
    funnel = {"invited": invited, "connected": connected, "sampled": samples, "dealed": deals["count"]}
    return {
        "date": day,
        "daily": {"invited": invited, "new_accounts": new_accounts, "connected": connected, "sampled": samples, "dealed": deals["count"], "deal_amount": deals["amount"], "overdue": overdue},
        "status_counts": status_counts,
        "funnel": funnel,
        "rates": {"connect_rate": round(connected / invited * 100, 1) if invited else 0, "sample_rate": round(samples / connected * 100, 1) if connected else 0, "deal_rate": round(deals["count"] / invited * 100, 1) if invited else 0},
        "owners": [dict(row) for row in owner_rows],
    }


@app.get("/api/crm/tasks")
def crm_tasks(context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with database() as db:
        today_rows = db.execute("SELECT account,nickname,next_follow_at,status FROM influencers WHERE tenant_id=? AND next_follow_at=? AND status NOT IN ('已出单','未建联') ORDER BY updated_at DESC LIMIT 20", (context.tenant_id, today)).fetchall()
        overdue_rows = db.execute("SELECT account,nickname,next_follow_at,status FROM influencers WHERE tenant_id=? AND next_follow_at<>'' AND next_follow_at<? AND status NOT IN ('已出单','未建联') ORDER BY next_follow_at LIMIT 20", (context.tenant_id, today)).fetchall()
        sampled_rows = db.execute("SELECT account,nickname,next_follow_at,status FROM influencers WHERE tenant_id=? AND status='寄样' ORDER BY updated_at DESC LIMIT 20", (context.tenant_id,)).fetchall()
    return {"today": [dict(r) for r in today_rows], "overdue": [dict(r) for r in overdue_rows], "sampled_not_dealed": [dict(r) for r in sampled_rows]}


@app.post("/api/crm/import-csv")
def crm_import_csv(body: ImportCsvRequest, context: Annotated[RequestContext, Depends(request_context)]) -> dict:
    reader = csv.DictReader(io.StringIO(body.csv_text.lstrip("\ufeff")))
    required = {"platform", "account"}
    if not reader.fieldnames or not required.issubset({name.strip() for name in reader.fieldnames}):
        raise HTTPException(422, "CSV 必须包含 platform 和 account 字段")
    created, skipped = 0, 0
    users_by_name = {}
    now = utc_now()
    with database() as db:
        for row in db.execute("SELECT id,display_name,username FROM crm_users WHERE tenant_id=?", (context.tenant_id,)).fetchall():
            users_by_name[row["id"]] = row["id"]
            users_by_name[row["display_name"]] = row["id"]
            users_by_name[row["username"]] = row["id"]
        for raw in reader:
            row = {str(k).strip(): (v or "").strip() for k, v in raw.items()}
            if not row.get("platform") or not row.get("account"):
                skipped += 1
                continue
            owner_id = users_by_name.get(row.get("owner", ""), context.user_id)
            try:
                influencer_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO influencers(id,tenant_id,platform,account,nickname,profile_url,category,followers,quote_price,contact,owner_id,status,next_follow_at,notes,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (influencer_id, context.tenant_id, row["platform"], row["account"], row.get("nickname", ""), row.get("profile_url", ""), row.get("category", ""), row.get("followers") or "0", float(row.get("quote_price") or 0), row.get("contact", ""), owner_id, row.get("status") or "未建联", row.get("next_follow_at", ""), row.get("notes", ""), now, now),
                )
                db.execute("INSERT INTO influencer_status_logs VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), context.tenant_id, influencer_id, None, row.get("status") or "未建联", context.user_id, now))
                created += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return {"created": created, "skipped": skipped}


@app.get("/api/crm/export-csv")
def crm_export_csv(context: Annotated[RequestContext, Depends(request_context)]) -> Response:
    clause, params = influencer_visible_clause(context)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "account", "nickname", "category", "followers", "quote_price", "contact", "owner", "status", "next_follow_at", "notes"])
    with database() as db:
        rows = db.execute(f"SELECT i.*,u.display_name owner_name FROM influencers i LEFT JOIN crm_users u ON u.id=i.owner_id WHERE {clause} ORDER BY i.updated_at DESC", params).fetchall()
        for row in rows:
            writer.writerow([row["platform"], row["account"], row["nickname"], row["category"], row["followers"], row["quote_price"], row["contact"], row["owner_name"] or "", row["status"], row["next_follow_at"], row["notes"]])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=influencers.csv"})


@app.get("/")
def frontend_index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/{filename:path}")
def frontend_asset(filename: str) -> FileResponse:
    allowed = {"styles.css", "app.js", "api.js", "influencer.html", "influencer.css", "influencer.js"}
    if filename not in allowed:
        raise HTTPException(404, "Not found")
    return FileResponse(ROOT / filename)
