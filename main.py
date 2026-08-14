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
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION 1.3
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

# We switch to Layer 1 (Recent Planning Applications) - Much more stable
LEEDS_PLANNING_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/1/query"
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

# ============================================================
# HELPERS
# ============================================================

def safe_string(value):
    return str(value).strip() if value else ""

def classify_tree_application(record):
    proposal = safe_string(record.get("PROPOSAL")).lower()
    if not proposal:
        return {"is_tree_related": False, "score": 0}

    matched = [k for k in STRONG_TREE_KEYWORDS if k in proposal]
    score = len(matched) * 2
    
    if any(word in proposal for word in ["fell", "felling", "remove", "removal"]):
        score += 5
    if "tpo" in proposal or "preservation order" in proposal:
        score += 5

    return {
        "is_tree_related": score >= 5,
        "score": score,
        "matched": matched
    }

# ============================================================
# DATA FETCHING (STABLE VERSION)
# ============================================================

def fetch_leeds_records(max_records=200):
    """
    Fetches records using a very lightweight query to prevent 400 errors.
    """
    # We only ask for the fields we absolutely need
    fields = "REFVAL,ADDRESS,PROPOSAL,DATEAPVAL,APPNAME"

    params = {
        "where": "1=1",
        "outFields": fields,
        "returnGeometry": "false",
        "resultRecordCount": max_records,
        "f": "json",
    }

    logger.info("Requesting lightweight data from Leeds Layer 1...")
    
    try:
        response = requests.get(LEEDS_PLANNING_URL, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            logger.error(f"Leeds API Error: {data['error']}")
            return []

        features = data.get("features", [])
        records = [f.get("attributes", {}) for f in features]
        
        # Sort them by date manually (newest first)
        records.sort(key=lambda x: x.get("DATEAPVAL", 0), reverse=True)
        return records

    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        return []

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent V1.3 Active"}

@app.get("/test-leeds")
def test_leeds():
    raw_records = fetch_leeds_records(max_records=200)
    
    # Only keep things from the last 90 days
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=90)
    
    valid_leads = []
    for r in raw_records:
        ms = r.get("DATEAPVAL")
        if not ms: continue
        
        app_date = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
        
        if app_date > cutoff_date:
            classification = classify_tree_application(r)
            if classification["is_tree_related"]:
                r["_score"] = classification["score"]
                r["_matched"] = classification["matched"]
                r["_date"] = app_date.strftime("%Y-%m-%d")
                valid_leads.append(r)

    return {
        "records_fetched": len(raw_records),
        "tree_leads_found": len(valid_leads),
        "leads": valid_leads
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_records = fetch_leeds_records(max_records=100)
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
        return {"status": "No leads today"}

    # Process best lead
    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    best = tree_apps[0]
    
    lead_data = extract_lead_with_openai(best)
    contractors = get_test_contractors()
    
    for c in contractors:
        try:
            session = create_test_checkout(c, lead_data, best.get("REFVAL"))
            send_tree_lead_email(c, lead_data, best.get("REFVAL"), session)
        except: pass

    return {"status": "Lead sent", "address": lead_data["site_address"]}

# ============================================================
# STABLE UTILITIES
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
    body = f"Hi {contractor['name']},\n\nNew Tree Job in {lead.get('postcode')}:\n{lead['scope_summary']}\n\nLink: {session.url}"
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
            if s
