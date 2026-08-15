import os, json, logging, requests, psycopg2, stripe, urllib3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from openai import OpenAI

# Standard logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

app = FastAPI(title="Vector Data Labs - Lead Management System", docs_url="/docs")

# --- ENVIRONMENT VARIABLES ---
# Ensure these are set in your hosting environment (e.g., Render)
OKEY = os.getenv("OPENAI_API_KEY")
SURL = os.getenv("SUPABASE_DB_URL")
S_SEC = os.getenv("STRIPE_SECRET_KEY")
S_WH = os.getenv("STRIPE_WEBHOOK_SECRET")
R_KEY = os.getenv("RESEND_API_KEY")
T_EM = os.getenv("TEST_EMAIL")
T_SEC = os.getenv("TRIGGER_SECRET")
P_URL = os.getenv("PUBLIC_APP_URL")

R_URL = "https://api.resend.com/emails"
client = OpenAI(api_key=OKEY)
stripe.api_key = S_SEC
_processed = set()

# --- SERVICE CONFIGURATION ---
# Note: For ArcGIS, the URL must exactly match the current production layer.
# For London, we use the official GLA Datahub REST API.
COUNCILS = {
    "Leeds_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    },
    "London_Datahub": {
        "type": "gla_api",
        "url": "https://planning.data.london.gov.uk/api/v1/applications/",
        "params": {"page_size": 50}
    },
    "Woking_Surrey": {
        "type": "arcgis",
        "url": "https://services2.arcgis.com/S96pW9S9VlU6z7fK/arcgis/rest/services/Planning_Applications/FeatureServer/0/query",
        "referer": "https://www.woking.gov.uk/"
    }
}

# --- LOGIC: CLASSIFICATION ---
TREE_WORDS = ["tree", "trees", "tpo", "felling", "fell", "crown", "pruning", "stump", "arboriculture", "oak", "ash ", "cedar", "conifer", "birch", "maple", "willow", "sycamore"]
SKIP_WORDS = ["dwelling", "erection of", "new build", "extension", "loft conversion", "demolition"]

def get_timestamp(record):
    """Extracts a valid float timestamp from various known date fields."""
    keys = ["DATE_RECEIVED", "received_date", "DATE_VALID", "DATEAPVAL", "RECDAT"]
    for key in keys:
        val = record.get(key)
        if val:
            try: return float(val)
            except (ValueError, TypeError): continue
    return 0

def classify_record(record):
    """Scores a record based on tree-related keywords."""
    description = str(
        record.get("development_description") or 
        record.get("description") or 
        record.get("PROPOSAL") or 
        record.get("DESCRIPTION") or ""
    ).lower()
    
    if not description:
        return False, 0
        
    matches = [word for word in TREE_WORDS if word in description]
    score = len(matches)
    
    if "tree" in description: score += 2
    if any(action in description for x in ["fell", "remove", "crown", "tpo"]): score += 5
    
    # Filter out major construction that only mentions trees incidentally
    if any(skip in description for skip in SKIP_WORDS) and score < 8:
        return False, 0
        
    return (score > 2), score

# --- FETCHING LOGIC ---
def fetch_data(name, config):
    """Generic fetcher that handles both ArcGIS and standard REST APIs."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    
    try:
        if config["type"] == "arcgis":
            headers["Referer"] = config["referer"]
            params = {
                "where": "1=1",
                "outFields": "*",
                "resultRecordCount": 50,
                "orderByFields": "OBJECTID DESC",
                "f": "json"
            }
            response = requests.get(config["url"], params=params, headers=headers, timeout=20)
        else:
            response = requests.get(config["url"], params=config.get("params"), headers=headers, timeout=20)

        if response.status_code != 200:
            return [], f"HTTP Error {response.status_code}"

        # Ensure the response is actually JSON before parsing
        if "application/json" not in response.headers.get("Content-Type", ""):
            return [], "Server returned non-JSON format (possible redirect or error page)"

        data = response.json()
        
        # Handle ArcGIS-specific error messages inside the JSON
        if "error" in data:
            return [], f"API Error: {data['error'].get('message')}"

        # Standardize record list output
        if config["type"] == "arcgis":
            records = [feat.get("attributes", {}) for feat in data.get("features", [])]
        else:
            records = data.get("results", [])
            
        return records, "Success"

    except Exception as e:
        return [], f"Connection Fail: {str(e)}"

# --- DATABASE OPERATIONS ---
def is_already_sent(ref):
    if not SURL: return False
    try:
        conn = psycopg2.connect(SURL)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sent_leads (ref TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        cur.execute("SELECT 1 FROM sent_leads WHERE ref = %s", (ref,))
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Database error: {e}")
        return False

def mark_as_sent(ref):
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL)
        cur = conn.cursor()
        cur.execute("INSERT INTO sent_leads (ref) VALUES (%s) ON CONFLICT DO NOTHING", (ref,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Database write error: {e}")

# --- ROUTES ---
@app.get("/test-regional")
def test_all():
    """Diagnostic tool to verify connectivity across all configured services."""
    results = {}
    for name, config in COUNCILS.items():
        records, status = fetch_data(name, config)
        leads = [r for r in records if classify_record(r)[0]]
        results[name] = {
            "status": status,
            "records_scanned": len(records),
            "potential_leads": len(leads)
        }
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    """Main process: Fetch, classify, and notify surgeons via Stripe/Resend."""
    if secret != T_SEC:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    total_processed = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000

    for c_name, config in COUNCILS.items():
        records, _ = fetch_data(c_name, config)
        
        for r in records:
            ref = str(r.get("REFERENCE") or r.get("reference") or r.get("OBJECTID"))
            is_valid, score = classify_record(r)
            
            if is_valid and (get_timestamp(r) >= cutoff or get_timestamp(r) == 0) and not is_already_sent(ref):
                # Process lead with AI and notify (Implementation omitted for brevity)
                # ... AI parsing, Stripe session creation, and Resend email logic ...
                mark_as_sent(ref)
                total_processed += 1
                if total_processed >= 10: break
        if total_processed >= 10: break
        
    return {"status": "success", "leads_identified": total_processed}

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h1>Vector Data Labs</h1><p>System Online. Check /docs for API details.</p>"
