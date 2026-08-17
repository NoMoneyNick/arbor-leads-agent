import os
import logging
import requests
import psycopg2
import urllib3
import re
import time
import math
import threading

from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


# ============================================================
# VECTOR DATA LABS
# V83.0 - LEEDS DISCOVERY + PERSISTENT RESEARCH STATUS
# ============================================================

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

app = FastAPI(
    title="Vector Data Labs - V83.0 Leeds Discovery",
    docs_url="/docs"
)

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(
    "vector-data-labs"
)


# ============================================================
# ENVIRONMENT
# ============================================================

SURL = os.getenv(
    "SUPABASE_DB_URL"
)

CH_KEY = os.getenv(
    "COMPANIES_HOUSE_KEY"
)

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk"
)


# ============================================================
# SEARCH TERMS
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

MAX_RESULTS_PER_TERM = 200

REQUEST_DELAY = 0.25

SERVICE_RADIUS_MILES = 15.0

LEEDS_LAT = 53.8008

LEEDS_LON = -1.5491


# ============================================================
# TREE TERMS
# ============================================================

TREE_TERMS = [
    "tree",
    "trees",
    "arbor",
    "arborist",
    "arboriculture",
    "forestry",
    "woodland",
    "stump",
    "treecare",
    "tree care",
    "tree surgeon",
    "tree surgery",
    "tree services",
    "tree service",
    "tree felling",
    "tree removal"
]


TREE_GOLD = [
    "tree",
    "trees",
    "tpo",
    "fell",
    "felling",
    "arboriculture",
    "arborist",
    "crown",
    "pruning",
    "prune",
    "stump",
    "oak",
    "ash",
    "willow",
    "cedar",
    "sycamore",
    "beech",
    "hedge",
    "woodland"
]


DESCRIPTION_FIELDS = [
    "proposal",
    "description",
    "development_description",
    "nature",
    "details",
    "PROPOSAL",
    "siteAddress",
    "site_address",
    "address"
]


# ============================================================
# RESEARCH STATE
# ============================================================

research_lock = threading.Lock()

research_running = False

research_started_at = None

research_finished_at = None

latest_research_result = None


# ============================================================
# DATABASE SETUP
# ============================================================

def init_db():

    if not SURL:

        logger.error(
            "SUPABASE_DB_URL is missing."
        )

        return

    try:

        conn = psycopg2.connect(
            SURL
        )

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

        cur.execute(
            """
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS postcode TEXT;
            """
        )

        cur.execute(
            """
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS distance_from_leeds_miles NUMERIC;
            """
        )

        cur.execute(
            """
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS service_area TEXT;
            """
        )

        cur.execute(
            """
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS tree_related_name BOOLEAN DEFAULT FALSE;
            """
        )

        cur.execute(
            """
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS search_term TEXT;
            """
        )

        conn.commit()

        cur.close()

        conn.close()

        logger.info(
            "Database schema verified and migrated."
        )

    except Exception as e:

        logger.exception(
            "Database migration error."
        )


init_db()


# ============================================================
# HTTP SESSION
# ============================================================

def get_ch_session():

    session = requests.Session()

    session.auth = (
        CH_KEY,
        ""
    )

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/83.0"
    })

    return session


# ============================================================
# POSTCODE CLEANING
# ============================================================

def clean_postcode(value):

    if not value:

        return None

    value = str(
        value
    ).upper().strip()

    value = re.sub(
        r"[^A-Z0-9 ]",
        "",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    match = re.search(
        r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b",
        value
    )

    if not match:

        return None

    return (
        f"{match.group(1)}"
        f"{match.group(2)}"
    )


# ============================================================
# EXTRACT POSTCODE
# ============================================================

def extract_postcode(address):

    if not address:

        return None

    address = str(
        address
    ).upper()

    match = re.search(
        r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b",
        address
    )

    if not match:

        return None

    return (
        f"{match.group(1)}"
        f"{match.group(2)}"
    )


# ============================================================
# POSTCODE COORDINATES
# ============================================================

def postcode_coordinates(
    session,
    postcode
):

    if not postcode:

        return None, None

    try:

        url = (
            "https://api.postcodes.io/postcodes/"
            + postcode
        )

        response = session.get(
            url,
            timeout=10
        )

        if response.status_code != 200:

            return None, None

        data = response.json()

        result = data.get(
            "result"
        )

        if not result:

            return None, None

        lat = result.get(
            "latitude"
        )

        lon = result.get(
            "longitude"
        )

        if lat is None or lon is None:

            return None, None

        return (
            float(lat),
            float(lon)
        )

    except Exception as e:

        logger.warning(
            f"Postcode lookup failed for "
            f"{postcode}: {e}"
        )

        return None, None


# ============================================================
# HAVERSINE
# ============================================================

def distance_miles(
    lat1,
    lon1,
    lat2,
    lon2
):

    earth_radius_km = 6371.0

    lat1 = math.radians(
        lat1
    )

    lat2 = math.radians(
        lat2
    )

    delta_lat = math.radians(
        lat2 - lat1
    )

    delta_lon = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(delta_lat / 2) ** 2
        +
        math.cos(lat1)
        *
        math.cos(lat2)
        *
        math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )

    kilometres = (
        earth_radius_km * c
    )

    return (
        kilometres * 0.621371
    )


# ============================================================
# LEEDS CORE POSTCODE
# ============================================================

def is_leeds_core_postcode(
    postcode
):

    if not postcode:

        return False

    postcode = (
        postcode
        .upper()
        .replace(" ", "")
    )

    return bool(
        re.match(
            r"^LS\d{1,2}[A-Z]{2}$",
            postcode
        )
    )


# ============================================================
# TREE COMPANY NAME TEST
# ============================================================

def name_looks_tree_related(
    name
):

    if not name:

        return False

    name = name.lower()

    return any(
        term in name
        for term in TREE_TERMS
    )


# ============================================================
# SERVICE AREA
# ============================================================

def classify_service_area(
    distance,
    is_core
):

    if distance is None:

        return "Unknown"

    if distance <= SERVICE_RADIUS_MILES:

        if is_core:

            return "Leeds Core"

        return "Leeds 15 Mile Service Area"

    return "Outside Service Area"


# ============================================================
# SAVE PARTNER
# ============================================================

def save_partner(
    cur,
    company,
    address,
    postcode,
    distance,
    service_area,
    tree_related_name,
    search_term
):

    company_number = company.get(
        "company_number"
    )

    if not company_number:

        return "invalid"


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


    if cur.fetchone():

        cur.execute(
            """
            UPDATE potential_partners
            SET
                status = %s,
                address = %s,
                postcode = %s,
                distance_from_leeds_miles = %s,
                service_area = %s,
                tree_related_name = %s,
                search_term = %s,
                last_verified = NOW(),
                updated_at = NOW()
            WHERE company_number = %s
            """,
            (
                company.get(
                    "company_status"
                ),
                address,
                postcode,
                distance,
                service_area,
                tree_related_name,
                search_term,
                company_number
            )
        )

        return "duplicate"


    cur.execute(
        """
        INSERT INTO potential_partners (
            company_name,
            company_number,
            status,
            date_incorporated,
            address,
            postcode,
            distance_from_leeds_miles,
            service_area,
            tree_related_name,
            operational_confidence,
            discovery_source,
            companies_house_url,
            last_verified,
            updated_at,
            search_term
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            NOW(),
            NOW(),
            %s
        )
        """,
        (
            company.get(
                "title"
            ),
            company_number,
            company.get(
                "company_status"
            ),
            company.get(
                "date_of_creation"
            ),
            address,
            postcode,
            distance,
            service_area,
            tree_related_name,
            50,
            "Companies House Leeds 15 Mile Discovery",
            (
                "https://find-and-update.company-information."
                "service.gov.uk/company/"
                + company_number
            ),
            search_term
        )
    )

    return "new"


# ============================================================
# EMPTY STATS
# ============================================================

def create_stats():

    return {
        "status": "success",
        "version": "83.0",
        "search_terms": 0,
        "pages_scanned": 0,
        "companies_examined": 0,
        "unique_companies_examined": 0,
        "active_companies": 0,
        "inactive_companies": 0,
        "valid_postcodes": 0,
        "postcode_lookup_errors": 0,
        "leeds_core_matches": 0,
        "within_15_mile_matches": 0,
        "outside_service_area": 0,
        "tree_named_companies": 0,
        "tree_named_leeds_matches": 0,
        "new_partners_added": 0,
        "duplicates_skipped": 0,
        "api_errors": 0,
        "sample_results": [],
        "sample_tree_leads": []
    }


# ============================================================
# COMPANIES HOUSE DISCOVERY
# ============================================================

def discover_leeds_partners():

    stats = create_stats()

    if not CH_KEY:

        stats["status"] = "error"

        stats["message"] = (
            "COMPANIES_HOUSE_KEY missing"
        )

        return stats


    if not SURL:

        stats["status"] = "error"

        stats["message"] = (
            "SUPABASE_DB_URL missing"
        )

        return stats


    session = get_ch_session()

    postcode_cache = {}

    seen_in_run = set()


    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()


        for search_term in SEARCH_TERMS:

            logger.info(
                f"Searching Companies House for: "
                f"{search_term}"
            )

            stats[
                "search_terms"
            ] += 1


            start_index = 0


            while (
                start_index
                < MAX_RESULTS_PER_TERM
            ):

                try:

                    response = session.get(
                        (
                            f"{COMPANIES_HOUSE_URL}"
                            f"/search/companies"
                        ),
                        params={
                            "q": search_term,
                            "items_per_page":
                                ITEMS_PER_PAGE,
                            "start_index":
                                start_index
                        },
                        timeout=20
                    )

                except Exception as e:

                    logger.error(
                        "Companies House "
                        f"request error: {e}"
                    )

                    stats[
                        "api_errors"
                    ] += 1

                    break


                if response.status_code != 200:

                    logger.error(
                        "Companies House "
                        f"returned HTTP "
                        f"{response.status_code}"
                    )

                    stats[
                        "api_errors"
                    ] += 1

                    break


                try:

                    data = response.json()

                except Exception:

                    stats[
                        "api_errors"
                    ] += 1

                    break


                items = data.get(
                    "items",
                    []
                )


                if not items:

                    break


                stats[
                    "pages_scanned"
                ] += 1


                for company in items:

                    stats[
                        "companies_examined"
                    ] += 1


                    company_number = (
                        company.get(
                            "company_number"
                        )
                    )


                    if not company_number:

                        continue


                    if company_number in seen_in_run:

                        continue


                    seen_in_run.add(
                        company_number
                    )


                    stats[
                        "unique_companies_examined"
                    ] += 1


                    status = company.get(
                        "company_status"
                    )


                    if status == "active":

                        stats[
                            "active_companies"
                        ] += 1

                    else:

                        stats[
                            "inactive_companies"
                        ] += 1


                    name = company.get(
                        "title",
                        ""
                    )


                    tree_related = (
                        name_looks_tree_related(
                            name
                        )
                    )


                    if tree_related:

                        stats[
                            "tree_named_companies"
                        ] += 1


                    address = company.get(
                        "address_snippet",
                        ""
                    )


                    postcode = (
                        extract_postcode(
                            address
                        )
                    )


                    if not postcode:

                        continue


                    stats[
                        "valid_postcodes"
                    ] += 1


                    if postcode in postcode_cache:

                        lat, lon = (
                            postcode_cache[
                                postcode
                            ]
                        )

                    else:

                        lat, lon = (
                            postcode_coordinates(
                                session,
                                postcode
                            )
                        )

                        postcode_cache[
                            postcode
                        ] = (
                            lat,
                            lon
                        )


                        if (
                            lat is None
                            or
                            lon is None
                        ):

                            stats[
                                "postcode_lookup_errors"
                            ] += 1

                            continue


                        time.sleep(
                            0.05
                        )


                    distance = (
                        distance_miles(
                            LEEDS_LAT,
                            LEEDS_LON,
                            lat,
                            lon
                        )
                    )


                    distance = round(
                        distance,
                        2
                    )


                    is_core = (
                        is_leeds_core_postcode(
                            postcode
                        )
                    )


                    if is_core:

                        stats[
                            "leeds_core_matches"
                        ] += 1


                    within_radius = (
                        distance
                        <= SERVICE_RADIUS_MILES
                    )


                    if within_radius:

                        stats[
                            "within_15_mile_matches"
                        ] += 1

                    else:

                        stats[
                            "outside_service_area"
                        ] += 1


                    service_area = (
                        classify_service_area(
                            distance,
                            is_core
                        )
                    )


                    if (
                        within_radius
                        and
                        tree_related
                    ):

                        stats[
                            "tree_named_leeds_matches"
                        ] += 1


                        if len(
                            stats[
                                "sample_tree_leads"
                            ]
                        ) < 30:

                            stats[
                                "sample_tree_leads"
                            ].append({
                                "company_name":
                                    name,
                                "company_number":
                                    company_number,
                                "status":
                                    status,
                                "address":
                                    address,
                                "postcode":
                                    postcode,
                                "distance_from_leeds_miles":
                                    distance,
                                "service_area":
                                    service_area,
                                "search_term":
                                    search_term
                            })


                    if (
                        within_radius
                        and
                        status == "active"
                    ):

                        result = save_partner(
                            cur=cur,
                            company=company,
                            address=address,
                            postcode=postcode,
                            distance=distance,
                            service_area=service_area,
                            tree_related_name=tree_related,
                            search_term=search_term
                        )


                        if result == "new":

                            stats[
                                "new_partners_added"
                            ] += 1


                        elif result == "duplicate":

                            stats[
                                "duplicates_skipped"
                            ] += 1


                    if len(
                        stats[
                            "sample_results"
                        ]
                    ) < 30:

                        stats[
                            "sample_results"
                        ].append({
                            "search_term":
                                search_term,
                            "company_name":
                                name,
                            "company_number":
                                company_number,
                            "status":
                                status,
                            "address":
                                address,
                            "postcode":
                                postcode,
                            "distance_from_leeds_miles":
                                distance,
                            "leeds_core":
                                is_core,
                            "within_15_miles":
                                within_radius,
                            "tree_related_name":
                                tree_related
                        })


                conn.commit()


                start_index += (
                    ITEMS_PER_PAGE
                )


                if len(items) < ITEMS_PER_PAGE:

                    break


                time.sleep(
                    REQUEST_DELAY
                )


        cur.close()

        conn.close()


        logger.info(
            "BACKGROUND RESEARCH FINISHED"
        )


        return stats


    except Exception as e:

        logger.exception(
            "Database save/discovery error"
        )

        stats["status"] = "error"

        stats["message"] = str(
            e
        )

        return stats


# ============================================================
# BACKGROUND WORKER
# ============================================================

def background_research():

    global research_running

    global research_finished_at

    global latest_research_result


    try:

        logger.info(
            "BACKGROUND RESEARCH STARTED"
        )


        result = (
            discover_leeds_partners()
        )


        latest_research_result = result

        research_finished_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )


    except Exception as e:

        logger.exception(
            "Background research failed"
        )

        latest_research_result = {
            "status": "error",
            "version": "83.0",
            "message": str(e)
        }


    finally:

        research_running = False


# ============================================================
# START RESEARCH
# ============================================================

def start_research():

    global research_running

    global research_started_at


    with research_lock:

        if research_running:

            return False


        research_running = True

        research_started_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )


        thread = threading.Thread(
            target=background_research,
            daemon=True
        )


        thread.start()


        return True


# ============================================================
# LEEDS COUNCIL
# ============================================================

def fetch_council():

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/83.0",
        "Referer":
            "https://www.leeds.gov.uk/"
    })


    url = (
        "https://mapservices.leeds.gov.uk/"
        "arcgis/rest/services/Public/"
        "Planning/MapServer/12/query"
    )


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
            url,
            params=params,
            timeout=20,
            verify=False
        )


        if response.status_code != 200:

            return [], "Offline"


        data = response.json()


        records = [
            feature.get(
                "attributes",
                {}
            )
            for feature in data.get(
                "features",
                []
            )
        ]


        return records, "Online"


    except Exception as e:

        logger.error(
            "Leeds Council request error: "
            f"{e}"
        )

        return [], "Fault"


# ============================================================
# COUNCIL DESCRIPTION
# ============================================================

def extract_council_description(
    record
):

    for key in record.keys():

        key_lower = str(
            key
        ).lower()


        for field in DESCRIPTION_FIELDS:

            if (
                field.lower()
                ==
                key_lower
            ):

                value = record.get(
                    key
                )

                if value:

                    return str(
                        value
                    ).strip()


    for key, value in record.items():

        if isinstance(
            value,
            str
        ):

            text = value.strip()


            if any(
                term in text.lower()
                for term in TREE_GOLD
            ):

                return text


    return ""


# ============================================================
# COUNCIL CLASSIFICATION
# ============================================================

def smart_classify(
    record
):

    description = (
        extract_council_description(
            record
        )
    )


    if not description:

        return (
            False,
            0,
            ""
        )


    text = description.lower()


    matches = []


    for word in TREE_GOLD:

        if word in text:

            matches.append(
                word
            )


    if not matches:

        return (
            False,
            0,
            description
        )


    score = (
        len(
            set(matches)
        )
        * 5
    )


    if any(
        x in text
        for x in [
            "fell",
            "felling",
            "tpo",
            "remove",
            "removal",
            "pruning"
        ]
    ):

        score += 15


    return (
        score >= 10,
        score,
        description
    )


# ============================================================
# COUNCIL LEADS
# ============================================================

def get_council_leads():

    records, status = (
        fetch_council()
    )


    leads = []


    for record in records:

        matched, score, description = (
            smart_classify(
                record
            )
        )


        if not matched:

            continue


        leads.append({
            "score": score,
            "summary": description,
            "record": record
        })


    return (
        records,
        status,
        leads
    )


# ============================================================
# HOME PAGE
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def lander():

    return """
    <html>

    <head>

        <title>
            Vector Data Labs
        </title>

    </head>

    <body style="
        font-family:Arial,sans-serif;
        text-align:center;
        padding-top:50px;
        background:#f4f4f9;
    ">

        <div style="
            display:inline-block;
            padding:45px;
            background:white;
            border-radius:15px;
            box-shadow:0 10px 30px
                rgba(0,0,0,0.1);
            border-top:6px solid #1b5e20;
            max-width:700px;
        ">

            <h1 style="
                color:#1b5e20;
            ">
                Vector Data Labs
            </h1>

            <h2>
                V83.0 Leeds Discovery
            </h2>

            <div style="
                background:#f1f8e9;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
            ">

                <b>
                    Business source:
                </b>
                Companies House
                <br>

                <b>
                    Core area:
                </b>
                Leeds LS postcodes
                <br>

                <b>
                    Extended area:
                </b>
                15-mile radius
                <br>

                <b>
                    Tree searches:
                </b>
                11 search categories
                <br>

                <b>
                    Research:
                </b>
                Background processing

            </div>

            <a href="/research-leeds"
               style="
                display:inline-block;
                padding:12px 25px;
                background:#1b5e20;
                color:white;
                text-decoration:none;
                border-radius:5px;
                font-weight:bold;
            ">

                Start Leeds Research

            </a>

            <br><br>

            <a href="/research-status"
               style="color:#555;">

                View Research Status

            </a>

            <br><br>

            <a href="/test-regional"
               style="color:#555;">

                Test Leeds Council Leads

            </a>

            <br><br>

            <a href="/docs"
               style="color:#555;">

                API Documentation

            </a>

        </div>

    </body>

    </html>
    """


# ============================================================
# START BUSINESS RESEARCH
# ============================================================

@app.get(
    "/research-leeds"
)
def run_discovery():

    started = start_research()


    if not started:

        return {
            "status":
                "already_running",

            "message":
                "Leeds business research "
                "is already running in "
                "the background.",

            "check":
                "/research-status"
        }


    return {
        "status":
            "started",

        "message":
            "Leeds business research "
            "has started in the "
            "background.",

        "check":
            "/research-status"
    }


# ============================================================
# RESEARCH STATUS
# ============================================================

@app.get(
    "/research-status"
)
def research_status():

    if research_running:

        return {
            "status":
                "running",

            "started_at":
                research_started_at,

            "message":
                "Research is currently "
                "running.",

            "check_again":
                "/research-status"
        }


    if latest_research_result is None:

        return {
            "status":
                "idle",

            "message":
                "No research run has "
                "completed yet.",

            "start":
                "/research-leeds"
        }


    return {
        "status":
            "finished",

        "started_at":
            research_started_at,

        "finished_at":
            research_finished_at,

        "results":
            latest_research_result
    }


# ============================================================
# COUNCIL TEST
# ============================================================

@app.get(
    "/test-regional"
)
def test_leeds():

    records, status, leads = (
        get_council_leads()
    )


    return {
        "council":
            "Leeds",

        "status":
            status,

        "records_checked":
            len(records),

        "leads_detected":
            len(leads),

        "leads":
            leads
    }


# ============================================================
# HEALTH
# ============================================================

@app.get(
    "/health"
)
def health():

    return {
        "status":
            "online",

        "version":
            "83.0",

        "database_configured":
            bool(SURL),

        "companies_house_configured":
            bool(CH_KEY),

        "service_area_miles":
            SERVICE_RADIUS_MILES,

        "research_running":
            research_running
    }


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn


    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )


    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
