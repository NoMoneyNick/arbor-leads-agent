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
# VECTOR DATA LABS - LEEDS PRODUCTION VERSION 1.9
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

TREE_KEYWORDS = [
    "tree", "trees", "tpo", "felling", "fell", "crown", "pruning", 
    "arboricultural", "stump", "birch", "oak", "sycamore", "ash", "cedar", 
    "conifer", "pollard", "reduction", "thinning", "conservation area"
]

SKIP_WORDS = ["dwelling", "extension", "new build", "erection of"]

# ============================================================
# HELPERS
# ============================================================

def safe_string(value):
    return str(value).strip() if value else ""

def get_best_date(record):
    """Checks the most common date fields used by Leeds Council."""
    val = record.get("DATEAPVAL") or record.get("DATE_RECEIVED") or record.get("DATEDECISS")
    try:
        return float(val) if val else 0
    except:
        return 0

def classify_tree_application(record):
    proposal = safe_string(record.get("PROPOSAL")).lower()
    if not proposal: return {"is_tree_related": False}

    matches = [k for k in TREE_KEYWORDS if k in proposal]
    
    score = len(matches)
    if "conservation area" in proposal and "tree" in proposal:
        score += 10
    if any(x in proposal for x in ["fell", "remove", "crown", "tpo"]):
        score += 5
        
    is_construction = any(w in proposal for w in SKIP_WORDS)

    if len(matches) > 0 and not is_construction:
        return {"is_tree_related": True, "score": score, "matched": matches}
    if score > 8: 
        return {"is_tree_related": True, "score": score, "matched": matches}
        
    return {"is_tree_related": False}

# ============================================================
# BATCH DATA FETCHING (PAGINATION)
# ============================================================

def fetch_single_batch(offset, size=150):
    """Fetches one small 'page' of data from Leeds."""
    target_fields = "REFVAL,ADDRESS,PROPOSAL,DATEAPVAL,DATE_RECEIVED"
    params = {
        "where": "1=1",
        "outFields": target_fields,
        "returnGeometry": "false",
        "resultRecordCount": size,
        "resultOffset": offset,
        "f": "json",
    }
    try:
        response = requests.get(LEEDS_PLANNING_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]
    except Exception as e:
        logger.error(f"Batch failed: {e}")
        return []

def fetch_leeds_records_full():
    """Fetches multiple batches and merges them."""
    all_records = []
    # Fetch 3 batches of 150 (450 total)
    for i in range(3):
        offset = i * 150
        batch = fetch_single_batch(offset)
        if not batch:
            break
        all_records.extend(batch)
        
    all_records.sort(key=lambda x: get_best_date(x), reverse=True)
    return all_records

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return {"status": "Leeds Tree Agent V1.9 Active"}

@app.get("/test-leeds")
def test_leeds():
    raw_records = fetch_leeds_records_full()
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp() * 1000
    
    valid_leads = []
    debug_dates = []
    
    for r in raw_records:
        date_val = get_best_date(r)
        if date_val > 0 and len(debug_dates) < 5:
            readable = datetime.fromtimestamp(date_val/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            debug_dates.append(readable)
        
        if date_val >= cutoff_ms:
            classification = classify_tree_application(r)
            if classification["is_tree_related"]:
                r["_score"] = classification["score"]
                r["_date"] = datetime.fromtimestamp(date_val/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                valid_leads.append(r)

    return {
        "total_downloaded": len(raw_records),
        "newest_dates_on_server": debug_dates,
        "tree_leads_found": len(valid_leads),
        "leads": valid_leads
    }

@app.get("/trigger-scrape")
def trigger_scrape(x_trigger_secret: str = Header(default=None)):
    if not TRIGGER_SECRET or x_trigger_secret != TRIGGER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_records = fetch_leeds_records_full()
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    
    tree_apps = []
    for r in raw_records:
        date_val = get_best_date(r)
        if date_val >= cutoff_ms:
            cls = classify_tree_application(r)
            if cls["is_tree_related"]:
                r["_score"] = cls["score"]
                tree_apps.append(r)

    if not tree_apps:
        return {"status": "No leads found today."}

    tree_apps.sort(key=lambda x: x["_score"], reverse=True)
    best = tree_apps[0]
    
    lead_data = extract_lead_with_openai(best)
    contractors = get_test_contractors()
    
    for c in contractors:
        try:
            session = create_test_checkout(c, lead_data, best.get("REFVAL"))
            send_tree_lead_email(c, lead_data, best.get("REFVAL"), session)
        except: pass

    return {"status": "Lead Sent", "address": lead_data["site_address"]}

# ============================================================
# STABLE UTILITIES
# ============================================================

def extract_lead_with_openai(record):
    # Using triple quotes to prevent SyntaxErrors during copy-paste
    raw_text = f"""
    Reference: {record.get('REFVAL')}
    Address: {record.get('ADDRESS')}
    Proposal: {record.get('PROPOSA
