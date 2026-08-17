import os
import logging
import requests
import psycopg2
import urllib3
import re
import time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query

# ============================================================
# VECTOR DATA LABS
# V82.0 - LEEDS DISCOVERY DIAGNOSTIC
#
# PURPOSE:
# 1. Test Companies House connectivity
# 2. See exactly what Companies House returns
# 3. Diagnose why Leeds businesses are not being captured
# 4. Keep Leeds Council tree-lead testing operational
#
# THIS VERSION DOES NOT TRY TO "GUESS" THE FIX.
# IT COLLECTS DIAGNOSTIC INFORMATION FIRST.
# ============================================================

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

app = FastAPI(
    title="Vector Data Labs - V82.0 Leeds Discovery Diagnostic",
    docs_url="/docs"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")


# ============================================================
# ENVIRONMENT
# ============================================================

SURL = os.getenv("SUPABASE_DB_URL")
T_SEC = os.getenv("TRIGGER_SECRET")
CH_KEY = os.getenv("COMPANIES_HOUSE_KEY")

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk"
)

LEEDS_COUNCIL_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)


# ============================================================
# DISCOVERY SETTINGS
# ============================================================

SEARCH_TERMS = [
    "tree",
    "tree services",
    "tree surgery",
    "tree surgeon",
    "arboriculture",
    "arborist",
    "tree care",
    "tree felling",
    "stump grinding",
    "forestry",
    "landscaping"
]

ITEMS_PER_PAGE = 50

# Diagnostic version deliberately limits the amount of data.
# We want to understand the API before doing a large search.
MAX_RESULTS_PER_TERM = 100

REQUEST_DELAY = 0.35


# ============================================================
# TREE CLASSIFICATION
# ============================================================

CABINET_HEADERS = [
    "proposal",
    "description",
    "development_description",
    "nature",
    "details",
    "PROPOSAL",
    "siteAddress",
    "address"
]

TREE_GOLD = [
    "tree",
    "tpo",
    "fell",
    "felling",
    "arboriculture",
    "crown",
    "pruning",
    "stump",
    "oak",
    "ash",
    "willow",
    "cedar"
]


# ============================================================
# DATABASE
# ============================================================

def init_db():

    if not SURL:
        logger.warning(
            "SUPABASE_DB_URL is missing. Database features disabled."
        )
        return

    try:

        conn = psycopg2.connect(SURL)
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS potential_partners (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                company_name TEXT,

                company_number TEXT UNIQUE,

                status TEXT,

                date_incorporated DATE,

                address TEXT,

                operational_confidence INT DEFAULT 0,

                created_at TIMESTAMPTZ DEFAULT NOW(),

                sic_codes TEXT[],

                discovery_source TEXT,

                companies_house_url TEXT,

                last_verified TIMESTAMPTZ,

                updated_at TIMESTAMPTZ
            );
            """
        )

        conn.commit()

        cur.close()
        conn.close()

        logger.info(
            "Database schema verified."
        )

    except Exception as e:

        logger.error(
            f"Database initialisation error: {e}"
        )


init_db()


# ============================================================
# COMPANIES HOUSE SESSION
# ============================================================

def get_ch_session():

    session = requests.Session()

    if CH_KEY:
        # Companies House API uses API key as username
        # with an empty password.
        session.auth = (
            CH_KEY,
            ""
        )

    session.headers.update(
        {
            "User-Agent":
                "VectorDataLabs/82.0 Leeds Business Research"
        }
    )

    return session


# ============================================================
# ADDRESS TESTING
# ============================================================

def address_contains_leeds(address):

    if not address:
        return False

    address_upper = address.upper()

    # Explicit city name
    if re.search(r"\bLEEDS\b", address_upper):
        return True

    # Leeds postcode districts
    #
    # Examples:
    # LS1
    # LS10
    # LS17
    # LS27
    #
    # We deliberately require LS + number.
    if re.search(
        r"\bLS\d{1,2}\b",
        address_upper
    ):
        return True

    return False


def postcode_match(address):

    if not address:
        return None

    match = re.search(
        r"\bLS\d{1,2}\b",
        address.upper()
    )

    if match:
        return match.group(0)

    return None


# ============================================================
# TREE NAME TEST
# ============================================================

def name_looks_tree_related(name):

    if not name:
        return False

    name_lower = name.lower()

    tree_terms = [
        "tree",
        "arbor",
        "arborist",
        "arboriculture",
        "forestry",
        "woodland",
        "stump"
    ]

    return any(
        term in name_lower
        for term in tree_terms
    )


# ============================================================
# COUNCIL CLASSIFICATION
# ============================================================

def smart_classify(record):

    combined_text = ""

    for key, value in record.items():

        if value is None:
            continue

        key_lower = key.lower()

        if any(
            header.lower() in key_lower
            for header in CABINET_HEADERS
        ):

            combined_text += " "
            combined_text += str(value).lower()

    if not combined_text.strip():
        return False, 0, []

    matched_terms = []

    for word in TREE_GOLD:

        if word in combined_text:

            matched_terms.append(word)

    if not matched_terms:

        return False, 0, []

    score = len(matched_terms) * 5

    if any(
        word in combined_text
        for word in [
            "fell",
            "felling",
            "tpo"
        ]
    ):

        score += 15

    return (
        score >= 10,
        score,
        matched_terms
    )


# ============================================================
# FIND HUMAN-READABLE COUNCIL DESCRIPTION
# ============================================================

def extract_summary(record):

    priority_fields = [
        "proposal",
        "PROPOSAL",
        "description",
        "development_description",
        "nature",
        "details"
    ]

    # First try exact useful fields
    for field in priority_fields:

        if field in record:

            value = record.get(field)

            if value:
                return str(value).strip()

    # Then try case-insensitive matching
    for key, value in record.items():

        if not value:
            continue

        if key.lower() in [
            "proposal",
            "description",
            "development_description",
            "nature",
            "details"
        ]:

            return str(value).strip()

    return ""


# ============================================================
# COMPANIES HOUSE DIAGNOSTIC SEARCH
# ============================================================

def discover_leeds_partners():

    if not CH_KEY:

        return {
            "status": "error",
            "message":
                "COMPANIES_HOUSE_KEY is missing"
        }

    stats = {

        "status": "success",

        "search_terms": 0,

        "pages_scanned": 0,

        "companies_examined": 0,

        "active_companies": 0,

        "inactive_companies": 0,

        "leeds_address_matches": 0,

        "active_leeds_candidates": 0,

        "tree_named_companies": 0,

        "new_partners_added": 0,

        "duplicates_skipped": 0,

        "api_errors": 0,

        "sample_results": [],

        "sample_leeds_matches": [],

        "sample_active_companies": []

    }

    session = get_ch_session()

    seen_in_run = set()

    conn = None
    cur = None

    try:

        if SURL:

            conn = psycopg2.connect(SURL)
            cur = conn.cursor()

        for term in SEARCH_TERMS:

            stats["search_terms"] += 1

            logger.info(
                f"Searching Companies House for: {term}"
            )

            start_index = 0

            results_for_term = 0

            while (
                start_index < MAX_RESULTS_PER_TERM
            ):

                try:

                    response = session.get(
                        f"{COMPANIES_HOUSE_URL}/search/companies",
                        params={
                            "q": term,
                            "items_per_page":
                                ITEMS_PER_PAGE,
                            "start_index":
                                start_index
                        },
                        timeout=20
                    )

                except Exception as e:

                    logger.error(
                        f"Request failed for '{term}': {e}"
                    )

                    stats["api_errors"] += 1

                    break

                if response.status_code != 200:

                    logger.error(
                        f"Companies House returned "
                        f"{response.status_code} "
                        f"for '{term}'"
                    )

                    stats["api_errors"] += 1

                    break

                try:

                    data = response.json()

                except Exception:

                    logger.error(
                        "Companies House returned "
                        "invalid JSON."
                    )

                    stats["api_errors"] += 1

                    break

                items = data.get(
                    "items",
                    []
                )

                if not items:
                    break

                stats["pages_scanned"] += 1

                for company in items:

                    stats[
                        "companies_examined"
                    ] += 1

                    company_number = company.get(
                        "company_number"
                    )

                    if not company_number:
                        continue

                    if company_number in seen_in_run:
                        continue

                    seen_in_run.add(
                        company_number
                    )

                    results_for_term += 1

                    name = company.get(
                        "title",
                        ""
                    )

                    status = company.get(
                        "company_status",
                        ""
                    )

                    address = company.get(
                        "address_snippet",
                        ""
                    )

                    creation_date = company.get(
                        "date_of_creation"
                    )

                    postcode = postcode_match(
                        address
                    )

                    is_leeds = (
                        address_contains_leeds(
                            address
                        )
                    )

                    is_active = (
                        status == "active"
                    )

                    tree_name = (
                        name_looks_tree_related(
                            name
                        )
                    )

                    if is_active:

                        stats[
                            "active_companies"
                        ] += 1

                    else:

                        stats[
                            "inactive_companies"
                        ] += 1

                    if is_leeds:

                        stats[
                            "leeds_address_matches"
                        ] += 1

                    if tree_name:

                        stats[
                            "tree_named_companies"
                        ] += 1

                    if (
                        is_active
                        and is_leeds
                    ):

                        stats[
                            "active_leeds_candidates"
                        ] += 1

                    # ----------------------------------------
                    # SAMPLE RAW RESULTS
                    # ----------------------------------------

                    if len(
                        stats["sample_results"]
                    ) < 20:

                        stats[
                            "sample_results"
                        ].append(
                            {
                                "search_term":
                                    term,
                                "company_name":
                                    name,
                                "company_number":
                                    company_number,
                                "status":
                                    status,
                                "address":
                                    address,
                                "date_of_creation":
                                    creation_date,
                                "leeds_address":
                                    is_leeds,
                                "postcode_match":
                                    postcode,
                                "tree_related_name":
                                    tree_name
                            }
                        )

                    # ----------------------------------------
                    # SAMPLE ACTIVE COMPANIES
                    # ----------------------------------------

                    if (
                        is_active
                        and len(
                            stats[
                                "sample_active_companies"
                            ]
                        ) < 20
                    ):

                        stats[
                            "sample_active_companies"
                        ].append(
                            {
                                "company_name":
                                    name,
                                "company_number":
                                    company_number,
                                "address":
                                    address,
                                "date_of_creation":
                                    creation_date,
                                "leeds_address":
                                    is_leeds,
                                "postcode_match":
                                    postcode,
                                "tree_related_name":
                                    tree_name
                            }
                        )

                    # ----------------------------------------
                    # SAMPLE LEEDS MATCHES
                    # ----------------------------------------

                    if (
                        is_leeds
                        and len(
                            stats[
                                "sample_leeds_matches"
                            ]
                        ) < 30
                    ):

                        stats[
                            "sample_leeds_matches"
                        ].append(
                            {
                                "company_name":
                                    name,
                                "company_number":
                                    company_number,
                                "status":
                                    status,
                                "address":
                                    address,
                                "date_of_creation":
                                    creation_date,
                                "postcode_match":
                                    postcode,
                                "tree_related_name":
                                    tree_name
                            }
                        )

                    # ----------------------------------------
                    # DATABASE
                    # ----------------------------------------

                    if (
                        is_active
                        and is_leeds
                        and cur
                    ):

                        cur.execute(
                            """
                            SELECT 1
                            FROM potential_partners
                            WHERE company_number = %s
                            """,
                            (
                                company_number,
                            )
                        )

                        exists = (
                            cur.fetchone()
                            is not None
                        )

                        if exists:

                            stats[
                                "duplicates_skipped"
                            ] += 1

                        else:

                            ch_url = (
                                "https://find-and-update."
                                "company-information."
                                "service.gov.uk/company/"
                                f"{company_number}"
                            )

                            cur.execute(
                                """
                                INSERT INTO
                                potential_partners
                                (
                                    company_name,
                                    company_number,
                                    status,
                                    address,
                                    date_incorporated,
                                    operational_confidence,
                                    discovery_source,
                                    companies_house_url,
                                    last_verified,
                                    updated_at
                                )
                                VALUES
                                (
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    NOW(),
                                    NOW()
                                )
                                """,
                                (
                                    name,
                                    company_number,
                                    status,
                                    address,
                                    creation_date,
                                    50,
                                    "Companies House Leeds Diagnostic",
                                    ch_url
                                )
                            )

                            stats[
                                "new_partners_added"
                            ] += 1

                # ----------------------------------------
                # PAGINATION
                # ----------------------------------------

                if len(items) < ITEMS_PER_PAGE:
                    break

                start_index += ITEMS_PER_PAGE

                time.sleep(
                    REQUEST_DELAY
                )

                if (
                    results_for_term
                    >= MAX_RESULTS_PER_TERM
                ):
                    break

            if cur and conn:

                conn.commit()

            time.sleep(
                REQUEST_DELAY
            )

        if cur:
            cur.close()

        if conn:
            conn.close()

        return stats

    except Exception as e:

        logger.exception(
            "Discovery error"
        )

        if cur:
            cur.close()

        if conn:
            conn.close()

        return {
            "status": "error",
            "message": str(e),
            "partial_results": stats
        }


# ============================================================
# LEEDS COUNCIL DATA
# ============================================================

def fetch_council():

    session = requests.Session()

    headers = {
        "User-Agent":
            "VectorDataLabs/82.0",
        "Referer":
            "https://www.leeds.gov.uk/"
    }

    try:

        params = {
            "where": "1=1",
            "outFields": "*",
            "resultRecordCount": 100,
            "orderByFields":
                "OBJECTID DESC",
            "f": "json"
        }

        response = session.get(
            LEEDS_COUNCIL_URL,
            params=params,
            headers=headers,
            timeout=20,
            verify=False
        )

        if response.status_code != 200:

            return [], (
                f"Offline ({response.status_code})"
            )

        data = response.json()

        features = data.get(
            "features",
            []
        )

        records = [
            feature.get(
                "attributes",
                {}
            )
            for feature in features
        ]

        return records, "Online"

    except Exception as e:

        logger.error(
            f"Leeds Council error: {e}"
        )

        return [], "Fault"


# ============================================================
# ROOT PAGE
# ============================================================

@app.get("/")
def lander():

    return """
    <html>

    <head>
        <title>Vector Data Labs</title>
    </head>

    <body
        style="
        font-family:sans-serif;
        text-align:center;
        padding-top:50px;
        background:#f4f4f9;
        "
    >

        <div
            style="
            display:inline-block;
            padding:50px;
            background:white;
            border-radius:15px;
            box-shadow:0 10px 30px rgba(0,0,0,0.1);
            border-top:6px solid #1b5e20;
            max-width:650px;
            "
        >

            <h1 style="color:#1b5e20;">
                Vector Data Labs
            </h1>

            <p>
                V82.0 Leeds Discovery Diagnostic
            </p>

            <div
                style="
                background:#fff9c4;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
                "
            >

                <b>Purpose:</b>
                Diagnose Companies House discovery.

                <br><br>

                <b>Target:</b>
                Leeds only.

                <br><br>

                <b>Mode:</b>
                Diagnostic — no assumptions.

            </div>

            <a
                href="/research-leeds"
                style="
                display:inline-block;
                padding:12px 25px;
                background:#1b5e20;
                color:white;
                text-decoration:none;
                border-radius:5px;
                font-weight:bold;
                "
            >
                Run Companies House Diagnostic
            </a>

            <br><br>

            <a
                href="/test-regional"
                style="color:#666;"
            >
                Check Leeds Council Tree Leads
            </a>

        </div>

    </body>

    </html>
    """


# ============================================================
# COMPANIES HOUSE RESEARCH ROUTE
# ============================================================

@app.get("/research-leeds")
def run_discovery():

    return discover_leeds_partners()


# ============================================================
# LEEDS COUNCIL TEST ROUTE
# ============================================================

@app.get("/test-regional")
def test_leeds():

    records, status = fetch_council()

    leads = []

    for record in records:

        is_tree, score, matched_terms = (
            smart_classify(record)
        )

        if is_tree:

            reference = (
                record.get("REFERENCE")
                or record.get("Reference")
                or record.get("APPLICATION_NUMBER")
                or record.get("OBJECTID")
            )

            address = (
                record.get("siteAddress")
                or record.get("SITEADDRESS")
                or record.get("address")
                or record.get("ADDRESS")
                or ""
            )

            summary = extract_summary(
                record
            )

            leads.append(
                {
                    "reference":
                        str(reference)
                        if reference
                        else None,

                    "address":
                        str(address),

                    "summary":
                        summary,

                    "classification_score":
                        score,

                    "matched_tree_terms":
                        matched_terms,

                    "source":
                        "Leeds City Council"
                }
            )

    return {
        "council":
            "Leeds",

        "status":
            status,

        "records_scanned":
            len(records),

        "tree_leads_detected":
            len(leads),

        "leads":
            leads
    }


# ============================================================
# SECURED TRIGGER
# ============================================================

@app.get("/trigger-scrape")
def trigger_scrape(
    secret: str = Query(...)
):

    if secret != T_SEC:

        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    records, status = fetch_council()

    leads = []

    for record in records:

        is_tree, score, matched_terms = (
            smart_classify(record)
        )

        if not is_tree:
            continue

        reference = (
            record.get("REFERENCE")
            or record.get("Reference")
            or record.get("APPLICATION_NUMBER")
            or record.get("OBJECTID")
        )

        address = (
            record.get("siteAddress")
            or record.get("SITEADDRESS")
            or record.get("address")
            or record.get("ADDRESS")
            or ""
        )

        summary = extract_summary(
            record
        )

        leads.append(
            {
                "reference":
                    str(reference)
                    if reference
                    else None,

                "address":
                    str(address),

                "summary":
                    summary,

                "score":
                    score,

                "matched_terms":
                    matched_terms,

                "source":
                    "Leeds City Council"
            }
        )

    return {
        "status":
            "success",

        "council_status":
            status,

        "leads_detected":
            len(leads),

        "leads":
            leads
    }
