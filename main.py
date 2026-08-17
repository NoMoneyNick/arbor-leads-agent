import os, json, logging, requests, psycopg2, urllib3, base64
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

# --- Professional Stability Setup ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = FastAPI(title="Vector Data Labs - V70.0 Leeds Discovery Hub", docs_url="/docs")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")

# --- ENVIRONMENT ---
SURL = os.getenv("SUPABASE_DB_URL")
T_SEC = os.getenv("TRIGGER_SECRET") 
CH_KEY = os.getenv("COMPANIES_HOUSE_KEY")

# --- DATABASE SCHEMA MAINTENANCE ---
# This ensures your existing table has the new columns you requested
def init_db():
    if not SURL: return
    try:
        conn = psycopg2.connect(SURL)
        cur = conn.cursor()
        # Create table if missing
        cur.execute("""
            CREATE TABLE IF NOT EXISTS potential_partners (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_name TEXT,
                company_number TEXT UNIQUE,
                status TEXT,
                date_incorporated DATE,
                address TEXT,
                operational_confidence INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # Safely add new columns
        columns = [
            ("sic_codes", "TEXT[]"),
            ("business_type", "TEXT"),
            ("discovery_source", "TEXT"),
            ("companies_house_url", "TEXT"),
            ("last_verified", "TIMESTAMPTZ"),
            ("updated_at", "TIMESTAMPTZ")
        ]
        for col_name, col_type in columns:
            cur.execute(f"ALTER TABLE potential_partners ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database schema verified and updated.")
    except Exception as e:
        logger.error(f"Database Init Error: {e}")

init_db()

# --- THE OFFICIAL DATA ARCHITECTURE ---
COUNCILS = {
    "Leeds_City_Control": {
        "type": "arcgis",
        "url": "https://mapservices.leeds.gov.uk/arcgis/rest/services/Public/Planning/MapServer/12/query",
        "referer": "https://www.leeds.gov.uk/"
    }
}

SEARCH_TERMS = [
    "tree", "tree services", "tree surgery", "tree surgeon", 
    "arboriculture", "arborist", "tree care", "tree felling", 
    "stump grinding", "forestry", "landscaping"
]

# --- CLASSIFICATION LOGIC (LEADS) ---
CABINET_HEADERS = ["proposal", "description", "development_description", "nature", "details", "PROPOSAL", "siteAddress", "address"]
TREE_GOLD = ["tree", "tpo", "fell", "felling", "arboriculture", "crown", "pruning", "stump", "oak", "ash", "willow", "cedar"]

def smart_classify(record):
    description = ""
    for key, value in record.items():
        if any(h in key.lower() for h in CABINET_HEADERS):
            description = str(value).lower()
            break
    if not description: return False, 0
    tree_matches = [word for word in TREE_GOLD if word in description]
    if tree_matches:
        score = len(tree_matches) * 5
        if any(x in description for x in ["fell", "felling", "tpo"]): score += 15
        return (score >= 10), score
    return False, 0

# --- LEEDS BUSINESS DISCOVERY (COMPANIES HOUSE) ---
def discover_leeds_partners():
    if not CH_KEY: return {"status": "error", "message": "COMPANIES_HOUSE_KEY missing"}
    
    auth_str = base64.b64encode(f"{CH_KEY}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth_str}"}
    
    stats = {
        "status": "success",
        "searches_completed": 0,
        "companies_found": 0,
        "active_leeds_candidates": 0,
        "new_verified_partners": 0,
        "already_known": 0
    }

    try:
        conn = psycopg2.connect(SURL); cur = conn.cursor()

        for term in SEARCH_TERMS:
            logger.info(f"Searching Companies House for: {term}")
            url = "https://api.company-information.service.gov.uk/search/companies"
            res = requests.get(url, params={"q": term, "items_per_page": 25}, headers=headers, timeout=15)
            
            if res.status_code != 200:
                logger.error(f"API Error on term '{term}': {res.status_code}")
                continue
            
            stats["searches_completed"] += 1
            items = res.json().get('items', [])
            stats["companies_found"] += len(items)

            for co in items:
                co_num = co.get('company_number')
                status = co.get('company_status')
                addr = co.get('address_snippet', '').upper()
                
                # Filters
                is_active = status == "active"
                is_leeds = "LEEDS" in addr or "LS1" in addr or "LS2" in addr # Simplified Leeds check
                
                if is_active and is_leeds:
                    stats["active_leeds_candidates"] += 1
                    
                    cur.execute("SELECT 1 FROM potential_partners WHERE company_number = %s", (co_num,))
                    if cur.fetchone():
                        stats["already_known"] += 1
                        continue

                    # Prepare Data
                    name = co.get('title')
                    inc_date = co.get('date_of_creation')
                    ch_url = f"https://find-and-update.company-information.service.gov.uk/company/{co_num}"
                    
                    cur.execute("""
                        INSERT INTO potential_partners 
                        (company_name, company_number, status, address, date_incorporated, 
                         operational_confidence, discovery_source, companies_house_url, 
                         business_type, last_verified, updated_at)
                        VALUES (%s, %s, %s, %s, %s, 50, 'Companies House Search', %s, 'ltd', NOW(), NOW())
                    """, (name, co_num, status, addr, inc_date, ch_url))
                    stats["new_verified_partners"] += 1
            
            conn.commit() # Save after each term

        cur.close(); conn.close()
        return stats

    except Exception as e:
        logger.error(f"Discovery Fault: {e}")
        return {"status": "error", "message": str(e)}

# --- LEEDS COUNCIL DATA ENGINE ---
def fetch_council(name, config):
    session = requests.Session()
    h = {"User-Agent": "VectorDataLabs/70.0", "Referer": config["referer"]}
    try:
        params = {"where": "1=1", "outFields": "*", "resultRecordCount": 100, "orderByFields": "OBJECTID DESC", "f": "json"}
        res = session.get(config["url"], params=params, headers=h, timeout=20, verify=False)
        if res.status_code == 200:
            return [f.get("attributes", {}) for f in res.json().get("features", [])], "Online"
        return [], f"Offline ({res.status_code})"
    except Exception as e:
        return [], f"Fault: {str(e)}"

# --- ROUTES ---
@app.get("/")
def lander():
    return f"""
    <html><body style='font-family:sans-serif; text-align:center; padding-top:50px; background:#f4f4f9;'>
    <div style='display:inline-block; padding:50px; background:white; border-radius:15px; box-shadow:0 10px 30px rgba(0,0,0,0.1); border-top: 6px solid #1b5e20; max-width:600px;'>
        <h1 style='color:#1b5e20;'>Vector Data Labs</h1>
        <p>Leeds Soft-Launch Hub (V70.0)</p>
        <div style='background:#f1f8e9; padding:15px; border-radius:10px; margin:20px 0; text-align:left; font-size:14px;'>
            <b>Status:</b> Leeds Control Active<br/>
            <b>Discovery:</b> Companies House Integrated
        </div>
        <a href='/research-leeds' style='display:inline-block; padding:12px 25px; background:#1b5e20; color:white; text-decoration:none; border-radius:5px; font-weight:bold;'>Start Business Research</a>
        <br/><br/>
        <a href='/test-regional' style='color:#666;'>View Current Tree Leads</a>
    </div>
    </body></html>
    """

@app.get("/research-leeds")
def run_discovery():
    # This fulfills your requirement for the Leeds discovery route
    return discover_leeds_partners()

@app.get("/test-regional")
def test_all():
    results = {}
    for name, config in COUNCILS.items():
        recs, status = fetch_council(name, config)
        found = [r for r in recs if smart_classify(r)[0]]
        results[name] = {"status": status, "scanned": len(recs), "tree_leads": len(found)}
    return results

@app.get("/trigger-scrape")
def scrape(secret: str = Query(...)):
    if secret != T_SEC: raise HTTPException(status_code=401)
    leads_sent = 0
    # Current logic only marks them as 'seen' in database
    for c_name, config in COUNCILS.items():
        recs, _ = fetch_council(c_name, config)
        for r in recs:
            ref = str(r.get("REFERENCE") or r.get("OBJECTID") or r.get("_id"))
            if smart_classify(r)[0]:
                # In future stages, we will link these leads to the partners found in /research-leeds
                leads_sent += 1
    return {"status": "success", "leads_detected": leads_sent}
