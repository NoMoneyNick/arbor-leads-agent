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
# V84.0 - LEEDS BUSINESS + COUNCIL LEAD DISCOVERY
# ============================================================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="Vector Data Labs - V84.0 Leeds Lead Discovery",
    docs_url="/docs"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vector-data-labs")


# ============================================================
# ENVIRONMENT
# ============================================================

SURL = os.getenv("SUPABASE_DB_URL")
CH_KEY = os.getenv("COMPANIES_HOUSE_KEY")

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk"
)


# ============================================================
# SEARCH CONFIGURATION
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
# RESEARCH STATE
# ============================================================

research_lock = threading.Lock()

research_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "business_research": None,
    "council_research": None,
    "error": None
}


# ============================================================
# TREE BUSINESS CLASSIFICATION
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


# ============================================================
# DATABASE SETUP + MIGRATION
# ============================================================

def init_db():

    if not SURL:
        logger.error("SUPABASE_DB_URL is missing.")
        return

    try:

        conn = psycopg2.connect(SURL)
        cur = conn.cursor()

        cur.execute("""
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
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS postcode TEXT;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS distance_from_leeds_miles NUMERIC;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS service_area TEXT;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS tree_related_name BOOLEAN
            DEFAULT FALSE;
        """)

        cur.execute("""
            ALTER TABLE potential_partners
            ADD COLUMN IF NOT EXISTS search_term TEXT;
        """)

        conn.commit()

        cur.close()
        conn.close()

        logger.info(
            "Database schema verified and migrated."
        )

    except Exception as e:

        logger.error(
            f"Database migration error: {e}"
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
        "User-Agent": "VectorDataLabs/84.0"
    })

    return session


# ============================================================
# POSTCODE CLEANING
# ============================================================

def clean_postcode(value):

    if not value:
        return None

    value = str(value).upper().strip()

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

    address = str(address).upper()

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
# POSTCODE -> COORDINATES
#
# IMPORTANT V84 FIX:
# Never allow failed coordinates to reach
# distance_miles().
# ============================================================

def postcode_coordinates(session, postcode):

    if not postcode:
        return None, None

    postcode = clean_postcode(postcode)

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

            logger.warning(
                f"Postcode API returned "
                f"HTTP {response.status_code} "
                f"for {postcode}"
            )

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

            logger.warning(
                f"Postcode API returned missing "
                f"coordinates for {postcode}"
            )

            return None, None

        try:

            lat = float(lat)
            lon = float(lon)

        except (
            TypeError,
            ValueError
        ):

            logger.warning(
                f"Invalid coordinates for "
                f"{postcode}: {lat}, {lon}"
            )

            return None, None

        return lat, lon

    except Exception as e:

        logger.warning(
            f"Postcode lookup failed "
            f"for {postcode}: {e}"
        )

        return None, None


# ============================================================
# HAVERSINE DISTANCE
# ============================================================

def distance_miles(
    lat1,
    lon1,
    lat2,
    lon2
):

    # --------------------------------------------------------
    # SAFETY GUARD
    # --------------------------------------------------------

    if (
        lat1 is None
        or lon1 is None
        or lat2 is None
        or lon2 is None
    ):

        return None

    try:

        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)

    except (
        TypeError,
        ValueError
    ):

        return None

    earth_radius_km = 6371.0

    lat1_rad = math.radians(
        lat1
    )

    lat2_rad = math.radians(
        lat2
    )

    delta_lat = math.radians(
        lat2 - lat1
    )

    delta_lon = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(
            delta_lat / 2
        ) ** 2
        +
        math.cos(lat1_rad)
        *
        math.cos(lat2_rad)
        *
        math.sin(
            delta_lon / 2
        ) ** 2
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
# LEEDS CORE POSTCODE TEST
# ============================================================

def is_leeds_core_postcode(postcode):

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

def name_looks_tree_related(name):

    if not name:
        return False

    name = name.lower()

    return any(
        term in name
        for term in TREE_TERMS
    )


# ============================================================
# SERVICE AREA CLASSIFICATION
# ============================================================

def classify_service_area(
    distance,
    is_core=False
):

    if is_core:
        return "Leeds Core"

    if distance is None:
        return "Unknown"

    if distance <= SERVICE_RADIUS_MILES:
        return (
            "Leeds 15 Mile Service Area"
        )

    return "Outside Service Area"


# ============================================================
# SAVE BUSINESS PARTNER
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
        (company_number,)
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
                last_verified = NOW(),
                updated_at = NOW(),
                search_term = %s
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
            company.get("title"),
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
                "https://find-and-update."
                "company-information.service.gov.uk/"
                f"company/{company_number}"
            ),
            search_term
        )
    )

    return "new"


# ============================================================
# COMPANIES HOUSE DISCOVERY
# ============================================================

def discover_leeds_partners():

    if not CH_KEY:

        return {
            "status": "error",
            "message": (
                "COMPANIES_HOUSE_KEY missing"
            )
        }

    if not SURL:

        return {
            "status": "error",
            "message": (
                "SUPABASE_DB_URL missing"
            )
        }

    stats = {
        "status": "success",
        "search_terms": 0,
        "pages_scanned": 0,
        "companies_examined": 0,
        "active_companies": 0,
        "inactive_companies": 0,
        "valid_postcodes": 0,
        "postcode_lookup_failures": 0,
        "coordinate_calculation_failures": 0,
        "leeds_core_matches": 0,
        "within_15_mile_matches": 0,
        "outside_service_area": 0,
        "tree_named_companies": 0,
        "new_partners_added": 0,
        "duplicates_updated": 0,
        "invalid_companies": 0,
        "api_errors": 0,
        "sample_results": [],
        "sample_leeds_matches": []
    }

    session = get_ch_session()

    postcode_cache = {}

    seen_in_run = set()

    conn = None
    cur = None

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

            stats["search_terms"] += 1

            start_index = 0

            while (
                start_index
                <
                MAX_RESULTS_PER_TERM
            ):

                try:

                    response = session.get(
                        (
                            f"{COMPANIES_HOUSE_URL}"
                            "/search/companies"
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
                        "Companies House returned "
                        f"HTTP {response.status_code}"
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

                        stats[
                            "invalid_companies"
                        ] += 1

                        continue

                    if (
                        company_number
                        in seen_in_run
                    ):
                        continue

                    seen_in_run.add(
                        company_number
                    )

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

                    # ------------------------------------------------
                    # POSTCODE CACHE
                    # ------------------------------------------------

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
                                "postcode_lookup_failures"
                            ] += 1

                            # ------------------------------------------------
                            # IMPORTANT:
                            # Continue to the next company.
                            # Do NOT crash the entire research run.
                            # ------------------------------------------------

                            continue

                        time.sleep(
                            0.05
                        )

                    # ------------------------------------------------
                    # SECOND SAFETY CHECK
                    # ------------------------------------------------

                    if (
                        lat is None
                        or
                        lon is None
                    ):

                        stats[
                            "coordinate_calculation_failures"
                        ] += 1

                        continue

                    # ------------------------------------------------
                    # DISTANCE
                    # ------------------------------------------------

                    distance = (
                        distance_miles(
                            LEEDS_LAT,
                            LEEDS_LON,
                            lat,
                            lon
                        )
                    )

                    if distance is None:

                        stats[
                            "coordinate_calculation_failures"
                        ] += 1

                        continue

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
                        <=
                        SERVICE_RADIUS_MILES
                    )

                    if within_radius:

                        stats[
                            "within_15_mile_matches"
                        ] += 1

                        service_area = (
                            classify_service_area(
                                distance,
                                is_core
                            )
                        )

                        # ------------------------------------------------
                        # ACTIVE COMPANIES ONLY
                        # ------------------------------------------------

                        if status == "active":

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
                                    "duplicates_updated"
                                ] += 1

                            elif result == "invalid":

                                stats[
                                    "invalid_companies"
                                ] += 1

                        # ------------------------------------------------
                        # SAMPLE LEEDS RESULTS
                        # ------------------------------------------------

                        if len(
                            stats[
                                "sample_leeds_matches"
                            ]
                        ) < 20:

                            stats[
                                "sample_leeds_matches"
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
                                "tree_related_name":
                                    tree_related
                            })

                    else:

                        stats[
                            "outside_service_area"
                        ] += 1

                    # ------------------------------------------------
                    # GENERAL SAMPLE
                    # ------------------------------------------------

                    if len(
                        stats[
                            "sample_results"
                        ]
                    ) < 20:

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

        return stats

    except Exception as e:

        logger.exception(
            "Database save/discovery error"
        )

        if conn:

            try:
                conn.rollback()
            except Exception:
                pass

        return {
            "status": "error",
            "message": str(e),
            "stats": stats
        }

    finally:

        if cur:

            try:
                cur.close()
            except Exception:
                pass

        if conn:

            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# LEEDS COUNCIL PLANNING DATA
# ============================================================

def fetch_council():

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/84.0",
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
            f"Leeds Council request error: {e}"
        )

        return [], "Fault"


# ============================================================
# COUNCIL TREE TERMS
# ============================================================

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
    "woodland",
    "removal",
    "remove",
    "cutting",
    "pollard",
    "pollarding",
    "crown reduction",
    "crown lift"
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
# COUNCIL DESCRIPTION EXTRACTION
# ============================================================

def extract_council_description(record):

    if not record:
        return ""

    # --------------------------------------------------------
    # First: likely description fields
    # --------------------------------------------------------

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

                    return (
                        str(value)
                        .strip()
                    )

    # --------------------------------------------------------
    # Second: inspect all text fields
    # --------------------------------------------------------

    for key, value in record.items():

        if not isinstance(
            value,
            str
        ):
            continue

        text = value.strip()

        if not text:
            continue

        text_lower = (
            text.lower()
        )

        if any(
            term in text_lower
            for term in TREE_GOLD
        ):

            return text

    return ""


# ============================================================
# COUNCIL SMART CLASSIFICATION
# ============================================================

def smart_classify(record):

    description = (
        extract_council_description(
            record
        )
    )

    if not description:

        return (
            False,
            0,
            "",
            []
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
            description,
            []
        )

    unique_matches = list(
        set(matches)
    )

    score = (
        len(unique_matches)
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
            "pruning",
            "prune",
            "pollard",
            "pollarding",
            "crown reduction",
            "crown lift"
        ]
    ):

        score += 15

    matched = (
        score >= 10
    )

    return (
        matched,
        score,
        description,
        unique_matches
    )


# ============================================================
# COUNCIL LEAD DISCOVERY
# ============================================================

def get_council_leads():

    records, status = (
        fetch_council()
    )

    leads = []

    for record in records:

        (
            matched,
            score,
            description,
            matched_terms
        ) = smart_classify(
            record
        )

        if not matched:
            continue

        lead = {
            "lead_type":
                "Tree Planning Lead",
            "score":
                score,
            "summary":
                description,
            "matched_terms":
                matched_terms,
            "record":
                record
        }

        leads.append(
            lead
        )

    return (
        records,
        status,
        leads
    )


# ============================================================
# COMPLETE RESEARCH JOB
# ============================================================

def complete_research_job():

    global research_state

    logger.info(
        "BACKGROUND RESEARCH STARTED"
    )

    business_result = None
    council_result = None
    error = None

    try:

        # ----------------------------------------------------
        # BUSINESS DISCOVERY
        # ----------------------------------------------------

        business_result = (
            discover_leeds_partners()
        )

        # ----------------------------------------------------
        # COUNCIL DISCOVERY
        # ----------------------------------------------------

        try:

            (
                council_records,
                council_status,
                council_leads
            ) = get_council_leads()

            council_result = {
                "status":
                    council_status,
                "records_checked":
                    len(council_records),
                "leads_detected":
                    len(council_leads),
                "leads":
                    council_leads
            }

        except Exception as e:

            logger.exception(
                "Council research error"
            )

            council_result = {
                "status":
                    "error",
                "message":
                    str(e),
                "records_checked":
                    0,
                "leads_detected":
                    0,
                "leads":
                    []
            }

    except Exception as e:

        logger.exception(
            "Background research error"
        )

        error = str(e)

    finally:

        with research_lock:

            research_state[
                "running"
            ] = False

            research_state[
                "finished_at"
            ] = datetime.now(
                timezone.utc
            ).isoformat()

            research_state[
                "business_research"
            ] = business_result

            research_state[
                "council_research"
            ] = council_result

            research_state[
                "error"
            ] = error

        logger.info(
            "BACKGROUND RESEARCH FINISHED"
        )


# ============================================================
# START BACKGROUND RESEARCH
# ============================================================

def start_research():

    global research_state

    with research_lock:

        if research_state[
            "running"
        ]:

            return False

        research_state = {
            "running":
                True,
            "started_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),
            "finished_at":
                None,
            "business_research":
                None,
            "council_research":
                None,
            "error":
                None
        }

    thread = threading.Thread(
        target=complete_research_job,
        daemon=True
    )

    thread.start()

    return True


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
            Vector Data Labs V84.0
        </title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

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
            box-shadow:
                0 10px 30px
                rgba(0,0,0,0.1);
            border-top:
                6px solid #1b5e20;
            max-width:700px;
        ">

            <h1 style="
                color:#1b5e20;
            ">
                Vector Data Labs
            </h1>

            <h2>
                V84.0 Leeds Lead Discovery
            </h2>

            <div style="
                background:#f1f8e9;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
                line-height:1.7;
            ">

                <b>Business source:</b>
                Companies House
                <br>

                <b>Core area:</b>
                Leeds LS postcodes
                <br>

                <b>Extended area:</b>
                15-mile radius from Leeds
                <br>

                <b>Planning source:</b>
                Leeds City Council
                <br>

                <b>Research:</b>
                Runs in background
                <br>

                <b>V84 safety:</b>
                Failed postcode lookups
                cannot crash research

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
                Start Leeds Research
            </a>

            <br><br>

            <a
                href="/research-status"
                style="color:#555;"
            >
                Check Research Status
            </a>

            <br><br>

            <a
                href="/test-regional"
                style="color:#555;"
            >
                Check Leeds Council Leads
            </a>

            <br><br>

            <a
                href="/health"
                style="color:#555;"
            >
                Health Check
            </a>

            <br><br>

            <a
                href="/docs"
                style="color:#555;"
            >
                API Documentation
            </a>

        </div>

    </body>

    </html>
    """


# ============================================================
# START RESEARCH ROUTE
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
                (
                    "Leeds business and "
                    "council research is "
                    "already running in "
                    "the background."
                ),
            "check":
                "/research-status"
        }

    return {
        "status":
            "started",
        "message":
            (
                "Leeds business and "
                "council research has "
                "started in the background."
            ),
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

    with research_lock:

        return {
            "status":
                (
                    "running"
                    if research_state[
                        "running"
                    ]
                    else "finished"
                ),
            "started_at":
                research_state[
                    "started_at"
                ],
            "finished_at":
                research_state[
                    "finished_at"
                ],
            "business_research":
                research_state[
                    "business_research"
                ],
            "council_research":
                research_state[
                    "council_research"
                ],
            "error":
                research_state[
                    "error"
                ]
        }


# ============================================================
# LEEDS COUNCIL TEST ROUTE
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
# HEALTH CHECK
# ============================================================

@app.get(
    "/health"
)
def health():

    with research_lock:

        running = (
            research_state[
                "running"
            ]
        )

    return {
        "status":
            "online",
        "version":
            "84.0",
        "database_configured":
            bool(SURL),
        "companies_house_configured":
            bool(CH_KEY),
        "service_area_miles":
            SERVICE_RADIUS_MILES,
        "research_running":
            running
    }


# ============================================================
# RUN LOCALLY
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
