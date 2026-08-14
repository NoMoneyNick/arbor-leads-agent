import os
import json
import logging
from datetime import datetime, timezone, timedelta

import requests
import psycopg2
from fastapi import FastAPI, Request, HTTPException, Header
from openai import OpenAI
import stripe


# ============================================================
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION 1.2
# ============================================================

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

TEST_EMAIL = os.getenv("TEST_EMAIL")
TRIGGER_SECRET = os.getenv("TRIGGER_SECRET")
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL")

RESEND_URL = "https://api.resend.com/emails"

# Layer 12 is the most reliable for Leeds Planning
LEEDS_PLANNING_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)

# ============================================================
# CLIENTS
# ============================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY
_processed_sessions_memory = set()

# ============================================================
# TREE KEYWORDS & LOGIC
# ============================================================

STRONG_TREE_KEYWORDS = [
    "tree", "trees", "tpo", "tree preservation order", "arboricultural", 
    "arboriculture", "arborist", "tree surgeon", "tree surgery", "tree works", 
    "tree work", "tree removal", "tree felling", "felling", "fell tree", 
    "fell trees", "crown reduction", "crown lifting", "crown thinning", 
    "pollard", "pruning", "stump grinding", "dead tree", "dangerous tree",
    "sycamore", "oak", "ash dieback", "conifer", "beech", "birch"
]

# Words that usually mean it's NOT a tree job
SKIP_WORDS = ["dwelling", "erection of", "extension", "new build", "apartments"]

# ============================================================
# HELPERS
# ============================================================

def safe_string(value):
    return str(value).strip() if value else ""

def milliseconds_to_date(value):
    if not value: return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except: return None

def classify_tree_application(record):
    """Determines if a record is actually about trees."""
    proposal = safe_string(record.get("PROPOSAL")).lower()
    
    if not proposal:
        return {"is_tree_related": False, "score": 0, "matched": []}

    matched = [k for k in STRONG_TREE_KEYWORDS if k in proposal]
    negatives = [n for n in SKIP_WORDS if n in proposal]

    score = len(matched) * 2
    
    # Significant tree work bonuses
    if any(word in proposal for word in ["fell", "felling", "remove", "removal"]):
        score += 5
    if "tpo" in proposal or "preservation order" in proposal:
        score += 5

    # If it mentions construction but also trees, we only take it if the score is very high
    is_tree_related = score >= 4
    if negatives and score < 10:
        is_tree_related = False

    return {
        "is_tree_related": is_tree_related,
        "score": score,
        "matched": matched
    }

# ============================================================
# DATA FETCHING (DUMB FETCH + SMART FILTER)
# ============================================================

def fetch_leeds_records(max_records=500):
    """
    Fetches the newest records from Leeds without a complex date filter
    to avoid 400 errors from their server.
    """
    params = {
        "where": "1=1", # Get everything
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": max_records,
        "orderByFields": "DATEAPVAL DESC", # Newest first
        "f": "json",
    }

    logger.info("Fetching the 500 newest records from Leeds Council...")
    
    try:
        response = requests.get(LEEDS_PLANNING_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            logger.error(f"Leeds API Error: {data['error']}")
            return []

        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]

    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        return []

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent Version 1.2 Active"}

@app.get("/test-leeds")
def test_leeds():
    # 1. Get raw records
    raw_records = fetch_leeds_records(max_records=500)
    
    # 2. Filter by date (last 60 days) in our own code
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=60)
    
    valid_leads = []
    
    for r in raw_records:
        # Get the date of the application
        ms = r.get("DATEAPVAL")
        if not ms: continue
        
        app_date = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
        
        # Only process if it's within 60 days
        if app_date > cutoff_date:
            classification = classify_tree_application(r)
            if classification["is_tree_related"]:
                r["_ai_score"] = classification["score"]
                r["_matched_keywords"] = classification["matched"]
                r["_readable_date"] = app_date.strftime("%Y-%m-%d")
                valid_leads.append(r)

    return {
        "records_scanned": len(raw_records),
        "tree_leads_found_last_60_days": len(valid_leads),
        "leads": valid_leads
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Get records and filter
    raw_records = fetch_leeds_records(max_records=200)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
    
    tree_apps = []
    for r in raw_records:
        ms = r.get("DATEAPVAL")
        if not ms: continue
        if datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc) > cutoff_date:
            cls = classify_tree_application(r)
            if cls["is_tree_related"]:
                r["_score"] = cls["score"]
                tree_apps.append(r)

    if not tree_apps:
        return {"status": "No tree leads found today."}

    # Process only the highest scoring lead
    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    selected_record = tree_apps[0]
    
    lead = extract_lead_with_openai(selected_record)
    contractors = get_test_contractors()
    results = []

    for c in contractors:
        try:
            session = create_test_checkout(c, lead, selected_record.get("REFVAL"))
            send_tree_lead_email(c, lead, selected_record.get("REFVAL"), session)
            results.append({"contractor": c["name"], "status": "sent"})
        except Exception as e:
            results.append({"contractor": c["name"], "status": "failed", "error": str(e)})

    return {"status": "Complete", "lead": lead["site_address"], "results": results}

# ============================================================
# STABLE UTILITIES (AI, STRIPE, EMAIL)
# ============================================================

def extract_lead_with_openai(record):
    raw_text = f"Ref: {record.get('REFVAL')}\nAddr: {record.get('ADDRESS')}\nProp: {record.get('PROPOSAL')}"
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON: applicant_name, site_address, postcode, scope_summary, high_value (bool)."},
            {"role": "user", "content": raw_text}
        ]
    )
    return json.loads(response.choices[0].message.content)

def create_test_checkout(contractor, lead, ref):
    amount = 4500 if lead.get("high_value") else 2500
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price_data": {"currency": "gbp", "product_data": {"name": f"Tree Lead: {lead.get('postcode', ref)}"}, "unit_amount": amount}, "quantity": 1}],
        mode="payment",
        success_url=f"{PUBLIC_APP_URL}/payment-success",
        cancel_url=f"{PUBLIC_APP_URL}/payment-cancelled",
        metadata={
            "surgeon_id": str(contractor["id"]),
            "postcode": lead.get("postcode", ""),
            "site_address": lead.get("site_address", ""),
            "application_reference": ref
        }
    )

def send_tree_lead_email(contractor, lead, ref, session):
    body = f"Hi {contractor['name']},\n\nNew Tree Job in {lead.get('postcode')}:\n{lead['scope_summary']}\n\nView Details & Buy: {session.url}"
    payload = {"from": "Vector Data Labs <onboarding@resend.dev>", "to": [contractor["email"]], "subject": f"New Lead: {ref}", "text": body}
    requests.post(RESEND_URL, json=payload, headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"})

def get_test_contractors():
    contractors = []
    if SUPABASE_DB_URL:
        try:
            conn = psycopg2.connect(SUPABASE_DB_URL)
            with conn.cursor() as cur:
                cur.execute("SELECT id, business_name, email FROM tree_surgeons WHERE active IS TRUE;")
                for r in cur.fetchall():
                    contractors.append({"id": r[0], "name": r[1], "email": r[2]})
            conn.close()
        except: pass
    if not contractors:
        contractors.append({"id": 1, "name": "Test User", "email": TEST_EMAIL})
    return contractors

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            if session["id"] not in _processed_sessions_memory:
                _processed_sessions_memory.add(session["id"])
                m = session["metadata"]
                msg = f"UNLOCKED: {m['application_reference']}\nAddress: {m['site_address']}"
                requests.post(RESEND_URL, json={"from": "Vector Data Labs <onboarding@resend.dev>", "to": [TEST_EMAIL], "subject": "Lead Paid!", "text": msg}, headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"})
    except: pass
    return {"status": "success"}

@app.get("/payment-success")
def payment_success(): return {"message": "Success"}

@app.get("/payment-cancelled")
def payment_cancelled(): return {"message": "Cancelled"}
