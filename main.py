import os, logging, requests, psycopg2, urllib3, re, time, math, threading
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# V84.0 - LEEDS DISCOVERY + STRUCTURED LEAD STORAGE
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V84.0 Surveyor Master", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
SURL = os.getenv("SUPABASE_DB_URL")
CH_KEY = os.getenv("COMPANIES_HOUSE_KEY")
LEEDS_LAT, LEEDS_LON = 53.8008, -1.5491
SERVICE_RADIUS_MILES = 15.0

# --- CONFIGURATION ---
SEARCH_TERMS = ["tree surgery", "tree surgeon", "arboriculture", "arborist", "tree care", "tree felling", "stump grinding", "forestry", "landscaping"]
TREE_GOLD = ["tree", "tpo", "fell", "felling", "arboriculture", "crown", "pruning", "stump", "oak", "ash", "willow", "cedar"]
DESCRIPTION_FIELDS = ["proposal", "description", "nature_of_work", "nature", "details"]

# --- BACKGROUND STATE ---
research_lock = threading.Lock()
research_state = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}

# --- DATABASE SETUP ---
def init_db():
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        # 1. Partner Table (The Workers)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS potential_partners (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_name TEXT, company_number TEXT UNIQUE, status TEXT, address TEXT,
                postcode TEXT, distance_from_leeds_miles NUMERIC, service_area TEXT,
                tree_related_name BOOLEAN DEFAULT FALSE, operational_confidence INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # 2. Leads Table (The Work) - V84 UPGRADE
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                reference TEXT UNIQUE, address TEXT, summary TEXT, 
                score INT, matched_terms TEXT[], status TEXT DEFAULT 'new',
                discovered_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit(); cur.close(); conn.close()
        logger.info("Database schema verified (V84.0 leads table active).")
    except Exception as e: logger.error(f"DB Init Error: {e}")

init_db()

# --- DEFENSIVE MATH ---
def distance_miles(lat1, lon1, lat2, lon2):
    """Safely calculate distance without crashing on bad data."""
    try:
        if None in [lat1, lon1, lat2, lon2]: return None
        radius = 3958.8 # Miles
        d_lat, d_lon = math.radians(float(lat2)-float(lat1)), math.radians(float(lon2)-float(lon1))
        a = math.sin(d_lat/2)**2 + math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) * math.sin(d_lon/2)**2
        return radius * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))
    except: return None

# --- LEAD SURVEYOR (CLASSIFICATION) ---
def extract_structured_lead(record):
    """The Surveyor: Extracts clean, professional details from raw council data."""
    combined_text = ""
    summary = "Work description not found"
    matched = []
    
    for key, value in record.items():
        val_str = str(value or "").lower()
        if any(h in key.lower() for h in DESCRIPTION_FIELDS + ["address", "siteaddress"]):
            combined_text += " " + val_str
            if key.lower() in DESCRIPTION_FIELDS: summary = val_str

    score = 0
    for word in TREE_GOLD:
        if word in combined_text:
            matched.append(word); score += 5
    if any(x in combined_text for x in ["fell", "felling", "tpo"]): score += 15

    return {
        "is_tree": score >= 10,
        "score": score,
        "summary": summary.capitalize(),
        "matched_terms": list(set(matched)),
        "reference": str(record.get("REFERENCE") or record.get("OBJECTID")),
        "address": record.get("ADDRESS") or record.get("SITE_ADDRESS") or "Leeds Area"
    }

# --- COUNCIL ENGINE ---
def fetch_leeds_leads():
    url = "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query"
    params = {"where": "1=1", "outFields": "*", "resultRecordCount": 50, "orderByFields": "OBJECTID DESC", "f": "json"}
    try:
        res = requests.get(url, params=params, timeout=20, verify=False)
        raw_records = [f.get("attributes", {}) for f in res.json().get("features", [])]
        processed_leads = []
        
        conn = psycopg2.connect(SURL); cur = conn.cursor()
        for rec in raw_records:
            lead = extract_structured_lead(rec)
            if lead["is_tree"]:
                cur.execute("""
                    INSERT INTO leads (reference, address, summary, score, matched_terms)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (reference) DO NOTHING RETURNING id;
                """, (lead["reference"], lead["address"], lead["summary"], lead["score"], lead["matched_terms"]))
                if cur.fetchone(): processed_leads.append(lead)
        conn.commit(); cur.close(); conn.close()
        return processed_leads
    except Exception as e:
        logger.error(f"Lead Fetch Error: {e}")
        return []

# --- BACKGROUND RESEARCHER (PARTNERS) ---
# [Note: This logic uses the existing background research process from V83.0]

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
def lander():
    return f"""<html><body style="font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;">
        <div style="display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border-top: 6px solid #1b5e20; max-width:650px;">
            <h1 style="color:#1b5e20;">Vector Data Labs</h1><p>V84.0 Surveyor Master</p>
            <div style="background:#f1f8e9; padding:15px; border-radius:10px; margin:20px 0; text-align:left; font-size:14px;">
                <b>Mode:</b> Background Processing Enabled<br/>
                <b>Leads:</b> Structured Storage Active
            </div>
            <a href="/research-leeds" style="display:inline-block; padding:12px 25px; background:#1b5e20; color:white; text-decoration:none; border-radius:5px; font-weight:bold;">Search Leeds Partners</a>
            <br/><br/>
            <a href="/test-regional" style="display:inline-block; padding:12px 25px; background:#333; color:white; text-decoration:none; border-radius:5px; font-weight:bold;">View Professional Leads</a>
        </div></body></html>"""

@app.get("/test-regional")
def test_leeds():
    leads = fetch_leeds_leads()
    return {"council": "Leeds", "leads_found": len(leads), "leads": leads}

# (Existing research-leeds and research-status routes remain here)
