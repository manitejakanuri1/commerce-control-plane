"""HTTP surface.

Three kinds of caller reach this service:

  buyers / AI buyers  authenticate with a merchant API key and ask to purchase
  Razorpay            posts signed webhooks to /webhooks/razorpay
  operators           read health and reconciliation status

The webhook route reads the raw request body before parsing, because the
signature covers the exact bytes Razorpay sent.
"""

import hashlib
import json
import logging
import time
from collections import defaultdict, deque

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import auth
import catalog
import config
import core
import db
import events
import keys
import payments
import policy_log
import retrieval
from orchestrator import propose_offer, resolve, start_purchase

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s",'
           '"logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("app")

app = FastAPI(title="Merchant Agent Commerce Control Plane", version="1.0.0")

# A storefront calls this API from the shopper's browser, so the browser needs
# permission to make the request at all.
#
# Note what this does not solve: a static site has nowhere to hide a secret, so
# any key it carries is public. CORS controls which origin may call, never who.
# That is what browse-scoped keys are for: a storefront key may search and ask
# for a proposal, and is refused by anything that moves money.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   config.ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

# One implementation, in keys.py. Two would eventually drift, and a hash that
# drifts means every existing key silently stops working.
hash_api_key = keys.hash_key


def _resolve_key(x_api_key):
    """Resolve an API key to a merchant and the scope that key carries.

    Keys are stored hashed, so a database dump does not hand over the ability
    to sell as somebody else.

    Two scopes exist because a static storefront has nowhere to hide a secret.
    Any key its pages carry is readable by anyone who views source, so that key
    must be able to browse and ask for a proposal, and must not be able to move
    money.
    """
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header required")

    digest = hash_api_key(x_api_key)
    row = db.query_one(
        "SELECT id, api_key_hash, browse_key_hash FROM merchants "
        "WHERE (api_key_hash = %s OR browse_key_hash = %s) AND active",
        (digest, digest))
    if row is None:
        raise HTTPException(401, "invalid API key")

    scope = "full" if digest == row["api_key_hash"] else "browse"
    return row["id"], scope


def authenticate(x_api_key: str = Header(None)):
    """Any valid key. Read paths only."""
    merchant_id, _ = _resolve_key(x_api_key)
    return merchant_id


def authenticate_full(x_api_key: str = Header(None)):
    """A full key. Required by anything that moves money or changes the
    catalog, so a leaked storefront key cannot do either."""
    merchant_id, scope = _resolve_key(x_api_key)
    if scope != "full":
        raise HTTPException(
            403, "this key is scoped to browse and propose only; a full "
                 "merchant key is required to purchase or change the catalog")
    return merchant_id


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------
# ponytail: in-process fixed window. Correct for a single instance; move the
# counters to Redis before running more than one replica.

_hits = defaultdict(deque)


def rate_limit(merchant_id):
    now = time.time()
    window = _hits[merchant_id]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "rate limit exceeded")
    window.append(now)


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class PurchaseRequest(BaseModel):
    buyer: str = Field(..., min_length=1, max_length=200)
    request: str = Field(..., min_length=1, max_length=1000)
    budget_paise: int | None = Field(None, ge=0)
    idempotency_key: str | None = Field(None, max_length=120)


class CatalogProduct(BaseModel):
    sku: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=300)
    description: str = Field("", max_length=4000)
    price_paise: int = Field(..., gt=0)
    # Optional, because no storefront publishes what it pays for stock and
    # requiring it blocks onboarding. Supply it and margin can be proven;
    # omit it and the discount cap plus any floor price still bind.
    cost_paise: int | None = Field(None, ge=0)
    # A derived number a merchant will share when they will not share cost:
    # "never sell this below X".
    floor_price_paise: int | None = Field(None, gt=0)
    stock: int = Field(..., ge=0)


class CatalogImport(BaseModel):
    products: list[CatalogProduct] = Field(..., min_length=1, max_length=5000)
    replace: bool = False


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    log.info("starting with config %s", json.dumps(config.summary()))
    if config.IS_PRODUCTION and not config.RAZORPAY_WEBHOOK_SECRET:
        raise RuntimeError(
            "RAZORPAY_WEBHOOK_SECRET must be set in production; without it "
            "webhook signatures cannot be verified")

    # Skipped in serverless, where "startup" happens on every cold start.
    # Migrating there would add latency to a request and let several instances
    # race each other over the same DDL. Deploys run migrations separately.
    if config.RUN_MIGRATIONS_ON_STARTUP:
        db.migrate()
    else:
        log.info("skipping migrations on startup (RUN_MIGRATIONS_ON_STARTUP)")


@app.on_event("shutdown")
def shutdown():
    db.close()


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

class SignUpRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=10, max_length=256)
    name: str = Field(..., min_length=1, max_length=120)
    website_name: str | None = Field(None, max_length=120)
    website_url: str | None = Field(None, max_length=300)


class SignInRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)


class RotateKeyRequest(BaseModel):
    scope: str = Field(..., pattern="^(full|browse)$")


class PolicyLogRecord(BaseModel):
    """One decision, as reported by a merchant's own policy engine.

    merchant_id is accepted so the payload round-trips unchanged, and then
    ignored — the key decides whose log this is. Everything here arrives over
    the network from software we cannot inspect, so every field is bounded.
    """
    sku: str = Field(..., min_length=1, max_length=64)
    result: str = Field(..., pattern="^(approved|refused)$")
    asked_bps: int = Field(..., ge=0, le=10000)
    allowed_bps: int = Field(..., ge=0, le=10000)
    failed_rules: list[str] = Field(default_factory=list, max_length=10)
    engine_version: str | None = Field(None, max_length=32)
    merchant_id: str | None = Field(None, max_length=64)
    at: str | None = Field(None, max_length=40)


SESSION_COOKIE = "ccp_session"


def _set_session(response, token):
    """httpOnly so page scripts cannot read it, which keeps a cross-site
    scripting bug from becoming a stolen session."""
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        secure=config.IS_PRODUCTION,
        samesite="lax",
        path="/")


def current_user(request: Request):
    user = auth.user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "not signed in")
    return user


@app.get("/")
def landing_page():
    """The public page, served by the API itself.

    Same origin as the endpoints it calls, so the browser needs no CORS grant
    and the whole product lives at one address rather than two that have to be
    kept in step.
    """
    page = Path(__file__).parent / "static" / "index.html"
    if not page.exists():
        raise HTTPException(404, "landing page not found")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/app")
def command_centre():
    """Where a signed-in user lands.

    The page itself is served to anyone; it then calls /v1/auth/me and sends
    visitors without a session to /login. Gating the HTML instead would mean a
    redirect before the page can explain itself, and there is nothing secret in
    the markup — the data behind it is what the session protects.
    """
    page = Path(__file__).parent / "static" / "app.html"
    if not page.exists():
        raise HTTPException(404, "command centre not found")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/login")
def login_page():
    page = Path(__file__).parent / "static" / "login.html"
    if not page.exists():
        raise HTTPException(404, "login page not found")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.post("/v1/auth/signup")
def signup(body: SignUpRequest, request: Request):
    try:
        _, issued = auth.sign_up(body.email, body.password, body.name,
                                 body.website_name, body.website_url)
        token, user = auth.sign_in(
            body.email, body.password,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc))

    # The only response that ever carries keys in the clear. After this they
    # exist as hashes and stubs, and a merchant who loses one rotates it.
    response = JSONResponse({
        "user": user,
        "keys": {
            "merchant_id": issued["merchant_id"],
            "full": issued["full_key"],
            "browse": issued["browse_key"],
            "shown_once": True,
            "note": "Store the full key on your server. The browse key is "
                    "safe in a web page; the full key is not.",
        },
    })
    _set_session(response, token)
    return response


@app.post("/v1/auth/login")
def login(body: SignInRequest, request: Request):
    try:
        token, user = auth.sign_in(
            body.email, body.password,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None)
    except auth.AuthError as exc:
        # 401, not 400: the credentials were understood and rejected.
        raise HTTPException(401, str(exc))

    response = JSONResponse({"user": user})
    _set_session(response, token)
    return response


@app.post("/v1/auth/logout")
def logout(request: Request):
    auth.sign_out(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/v1/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------
# Session-authenticated, not key-authenticated. A key must never be able to
# read or rotate itself: that would turn one leaked storefront key into
# permanent access, because the attacker could rotate the merchant out of
# their own account.

@app.get("/v1/keys")
def list_keys(user: dict = Depends(current_user)):
    if not user["merchant_id"]:
        raise HTTPException(409, "this account has no merchant yet")
    return keys.describe(user["merchant_id"])


@app.post("/v1/keys/rotate")
def rotate_key(body: RotateKeyRequest, user: dict = Depends(current_user)):
    if not user["merchant_id"]:
        raise HTTPException(409, "this account has no merchant yet")
    try:
        new_key = keys.rotate(user["merchant_id"], body.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))

    return {
        "scope": body.scope,
        "key": new_key,
        "shown_once": True,
        "note": "The previous key stopped working the moment this was "
                "issued. Update anywhere it was in use.",
    }


# --------------------------------------------------------------------------
# policy engine reports
# --------------------------------------------------------------------------
# The engine is a package running on the merchant's own server. It posts a
# summary of each decision here and never waits for the answer, so this route
# may be slow or down without touching anybody's checkout.
#
# A full key is required. The engine is server-side by definition, and a
# browse key arriving here means a merchant has pasted their public key into a
# server config — which is worth refusing loudly rather than accepting.

@app.post("/v1/policy/logs")
def receive_policy_log(body: PolicyLogRecord,
                       merchant_id: str = Depends(authenticate_full),
                       x_engine_version: str = Header(None)):
    try:
        policy_log.check_version(x_engine_version or body.engine_version)
    except policy_log.VersionTooOld as exc:
        # 426 Upgrade Required. The package watches for this status
        # specifically and prints the upgrade command.
        raise HTTPException(426, str(exc))

    try:
        return policy_log.record(merchant_id, body.model_dump(),
                                 engine_version=x_engine_version)
    except policy_log.InvalidRecord as exc:
        raise HTTPException(400, str(exc))


@app.get("/v1/policy/summary")
def policy_summary(days: int = 7,
                   merchant_id: str = Depends(authenticate)):
    return policy_log.summary(merchant_id, days)


# --------------------------------------------------------------------------
# what shoppers asked for
# --------------------------------------------------------------------------

@app.get("/v1/events/demand")
def unmet_demand(days: int = 30, limit: int = 20,
                 merchant_id: str = Depends(authenticate)):
    """Searches that returned nothing, grouped.

    A merchant cannot get this anywhere else. Their own search returned "no
    results" and forgot; the payment processor never saw these shoppers at
    all, because they left before checkout.
    """
    return {"unmet_demand": events.unmet_demand(merchant_id, days, limit)}


@app.get("/v1/events/summary")
def events_summary(days: int = 7,
                   merchant_id: str = Depends(authenticate)):
    return events.summary(merchant_id, days)


@app.get("/demo")
def demo_page():
    """Agent search, served from the API itself.

    Same origin as the endpoints it calls, so the browser needs no CORS grant
    for the demo to run. A real storefront lives on its own domain and does
    need one, which is what ALLOWED_ORIGINS is for.
    """
    page = Path(__file__).parent / "static" / "demo.html"
    if not page.exists():
        raise HTTPException(404, "demo page not found")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    database_ok = db.healthy()
    body = {
        "ok": database_ok,
        "environment": config.ENVIRONMENT,
        "database": database_ok,
        "payments": payments.mode(),
        "retrieval": retrieval.health(),
    }
    return JSONResponse(body, status_code=200 if database_ok else 503)


@app.post("/v1/propose")
def propose(body: PurchaseRequest, merchant_id: str = Depends(authenticate)):
    """Ask what the merchant would sell, and whether policy allows it.

    Nothing is committed: no stock held, no payment created. That is what makes
    it safe for a storefront key, and it is the endpoint a shop's search box
    should call.
    """
    rate_limit(merchant_id)
    result = propose_offer(merchant_id, body.request, body.budget_paise)

    response = {
        "approved": result.ok,
        "stage": result.stage,
        "message": result.message,
    }
    decision = getattr(result, "decision", None)
    if decision:
        response["policy"] = [
            {"rule": c["rule"], "authority": c["authority"],
             "status": c["status"], "passed": c["passed"],
             "detail": c["detail"]}
            for c in decision["checks"]
        ]
    if result.ok:
        response["quote"] = result.quote
        response["rationale"] = result.proposal.get("rationale")
    return response


@app.post("/v1/purchase")
def purchase(body: PurchaseRequest,
             merchant_id: str = Depends(authenticate_full)):
    """Intent in, payable Razorpay order out.

    A refusal is a 200 with approved=false, not an error: the policy engine
    declining an offer is the system working, and the caller needs to see the
    reasons rather than a stack trace.
    """
    rate_limit(merchant_id)

    result = start_purchase(
        merchant_id=merchant_id,
        buyer=body.buyer,
        request=body.request,
        budget_paise=body.budget_paise,
        idempotency_key=body.idempotency_key,
    )

    response = {
        "approved": result.ok,
        "stage": result.stage,
        "message": result.message,
        "order_id": getattr(result, "order_id", None),
    }

    decision = getattr(result, "decision", None)
    if decision:
        response["policy"] = [
            {"rule": c["rule"], "authority": c["authority"],
             "passed": c["passed"], "detail": c["detail"]}
            for c in decision["checks"]
        ]

    if result.ok and hasattr(result, "quote"):
        response["quote"] = result.quote
        response["razorpay_order_id"] = result.rp_order_id
        response["razorpay_key_id"] = config.RAZORPAY_KEY_ID or None

    return response


@app.post("/v1/catalog")
def import_catalog(body: CatalogImport,
                   merchant_id: str = Depends(authenticate_full)):
    """Load a merchant's products.

    This is the whole onboarding path. A storefront exports its products, an
    adapter maps them into this shape, and the merchant becomes sellable to an
    AI buyer without changing anything about their own site.
    """
    rate_limit(merchant_id)
    try:
        result = catalog.import_products(
            merchant_id,
            [p.model_dump() for p in body.products],
            replace=body.replace)
    except catalog.InvalidProduct as exc:
        raise HTTPException(422, str(exc))
    return result


@app.get("/v1/catalog/search")
def search_catalog(q: str = "", limit: int = 20,
                   merchant_id: str = Depends(authenticate)):
    """What an AI buyer sees before it proposes anything.

    Returns prices, never costs. cost_paise is the merchant's own figure and
    has no business leaving the system.
    """
    rate_limit(merchant_id)

    started = time.perf_counter()
    products = catalog.browse(merchant_id, q or None, limit)

    # A search that found nothing is the most useful row in the events table:
    # it is demand the merchant is failing to meet, and it is invisible
    # everywhere else. Recording it can never fail the search.
    if q:
        events.record(merchant_id, "search", query=q, results=len(products),
                      duration_ms=int((time.perf_counter() - started) * 1000))

    return {"products": products}


@app.get("/v1/orders/{order_id}")
def get_order(order_id: str, merchant_id: str = Depends(authenticate_full)):
    order = core.get_order(order_id)
    if order is None or order["merchant_id"] != merchant_id:
        raise HTTPException(404, "order not found")
    return order


@app.post("/v1/orders/{order_id}/reconcile")
def force_reconcile(order_id: str, merchant_id: str = Depends(authenticate_full)):
    """Operator handle. Reads provider state; never creates a payment."""
    order = core.get_order(order_id)
    if order is None or order["merchant_id"] != merchant_id:
        raise HTTPException(404, "order not found")
    return {"result": resolve(order_id)}


@app.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
):
    raw = await request.body()

    if not payments.verify_signature(raw, x_razorpay_signature):
        core.audit("WEBHOOK_REJECTED", {"reason": "signature mismatch"})
        raise HTTPException(400, "invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json")

    fresh, event_id = payments.record_webhook(payload)
    if not event_id:
        raise HTTPException(400, "missing event id")

    # Acknowledge duplicates with 200. A non-2xx makes Razorpay retry, and
    # retrying an event already handled adds load for no benefit.
    if not fresh:
        return {"status": "duplicate ignored", "event_id": event_id}

    try:
        outcome = payments.process_webhook(payload)
    except Exception as exc:                    # noqa: BLE001
        # Already durably recorded, so let the retry worker pick it up rather
        # than losing the event.
        log.exception("webhook processing failed for %s", event_id)
        payments._mark_processed(event_id, error=str(exc))
        return {"status": "accepted, queued for retry", "event_id": event_id}

    return {"status": outcome, "event_id": event_id}


@app.post("/v1/ops/sweep")
def run_sweep(x_cron_secret: str = Header(None)):
    """Reconcile every stuck order, and retry webhooks that failed to process.

    workers.py does this on a timer when the service runs as a process. In a
    serverless deployment there is no timer, so a scheduler calls this instead.
    Without it, an order whose webhook never arrived would sit unresolved until
    somebody noticed — which is the exact failure this system exists to avoid.

    Guarded by a shared secret rather than a merchant key: it operates across
    all merchants, so no single merchant's credential should authorise it.
    """
    if not config.CRON_SECRET:
        raise HTTPException(503, "CRON_SECRET is not configured")
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(401, "invalid cron secret")

    retried = payments.retry_failed_webhooks()
    resolved = payments.sweep()
    pressure = core.reconciliation_pressure(window_minutes=15)

    if pressure >= 5:
        log.error("ALERT reconciliation ran %s times in 15 minutes, "
                  "webhook delivery is degraded", pressure)

    # Telemetry retention rides along with the sweep rather than needing its
    # own schedule. It must never stop the reconciliation half of this route,
    # which is the half that protects money.
    try:
        pruned = events.prune()
    except Exception as exc:                              # noqa: BLE001
        log.warning("event pruning failed (%s: %s)", type(exc).__name__, exc)
        pruned = None

    return {
        "webhooks_retried": len(retried),
        "orders_reconciled": len(resolved),
        "outcomes": resolved,
        "reconciliations_last_15m": pressure,
        "degraded": pressure >= 5,
        "events_pruned": pruned,
    }


@app.get("/v1/ops/reconciliation")
def reconciliation_status(merchant_id: str = Depends(authenticate_full)):
    """The audit trail as a feedback loop rather than a logbook."""
    pressure = core.reconciliation_pressure(window_minutes=15)
    stuck = core.unresolved_orders(config.RECONCILE_STALE_AFTER_SECONDS)
    ok, broken_at = core.verify_audit_chain()
    return {
        "reconciliations_last_15m": pressure,
        "degraded": pressure >= 5,
        "orders_awaiting_resolution": len(stuck),
        "audit_chain_intact": ok,
        "audit_chain_broken_at": broken_at,
    }
