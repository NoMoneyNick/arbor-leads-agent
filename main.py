import os
import logging
import requests
import psycopg2
import urllib3
import re
import time
import math

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


# ============================================================
# VECTOR DATA LABS
# V83.0 - LEEDS + 15 MILE SERVICE AREA DISCOVERY
#
# V83 FIXES:
# - Correct Haversine distance calculation
# - Safe handling of failed postcode lookups
# - Never crashes because lat/lon is None
# - Correct 15-mile geographic filtering
# - Improved tree-company name filtering
# - Database postcode migration retained
# ============================================================

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

app = FastAPI(
    title="Vector Data Labs - V83.0 Leeds Discovery",
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
# COMPANIES HOUSE SEARCH TERMS
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


# ============================================================
# LEEDS SERVICE AREA
# ============================================================

SERVICE_RADIUS_MILES = 15.0

LEEDS_LAT = 53.8008
LEEDS_LON = -1.5491


# ============================================================
# TREE CLASSIFICATION
# ============================================================

TREE_TERMS = [
    "arborist",
    "arboriculture",
    "tree surgeon",
    "tree surgery",
    "tree services",
    "tree service",
    "tree care",
    "tree felling",
    "tree removal",
    "stump grinding",
    "stump removal",
    "forestry",
    "woodland"
]


# Terms that are useful when appearing as standalone words.
#
# "tree" is deliberately handled separately because searching
# Companies House for "tree" produces false positives such as:
#
# TREE ACCOUNTANCY LIMITED
# TREE ADVISORY GROUP LIMITED
# TREE AID
#
# A company called "Tree & Garden Services" is relevant, but
# "Tree Accountancy" is not.
# ============================================================

TREE_STANDALONE_RELEVANT_PATTERNS = [
    r"\btree\s+(services?|surgery|surgeon|care|felling|removal)\b",
    r"\btree\s*&\s*(garden|ground|landscape|woodland|hedge)\b",
    r"\btree\s+and\s+(garden|ground|landscape|woodland|hedge)\b",
    r"\bstump\s+(grinding|removal)\b",
    r"\barborist\b",
    r"\barboriculture\b",
    r"\bforestry\b",
    r"\bwoodland\b"
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

        # ----------------------------------------------------
        # Safe migrations for existing Supabase database
        # ----------------------------------------------------

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
# COMPANIES HOUSE SESSION
# ============================================================

def get_ch_session():

    session = requests.Session()

    session.auth = (
        CH_KEY,
        ""
    )

    session.headers.update({
        "User-Agent": "VectorDataLabs/83.0"
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
# EXTRACT POSTCODE FROM ADDRESS
# ============================================================

def extract_postcode(address):

    if not address:
        return None

    return clean_postcode(address)


# ============================================================
# POSTCODE -> COORDINATES
#
# Uses postcodes.io.
#
# IMPORTANT:
# Failed lookups return (None, None).
# The caller MUST check both values before calculating distance.
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

        result = data.get("result")

        if not result:
            return None, None

        lat = result.get("latitude")
        lon = result.get("longitude")

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
# HAVERSINE DISTANCE
#
# FIXED IN V83.
#
# The previous version incorrectly converted latitude/longitude
# values multiple times, producing wildly incorrect distances.
# ============================================================

def distance_miles(
    lat1,
    lon1,
    lat2,
    lon2
):

    # Never allow invalid coordinates through.
    if (
        lat1 is None
        or lon1 is None
        or lat2 is None
        or lon2 is None
    ):
        return None

    try:

        lat1_rad = math.radians(
            float(lat1)
        )

        lon1_rad = math.radians(
            float(lon1)
        )

        lat2_rad = math.radians(
            float(lat2)
        )

        lon2_rad = math.radians(
            float(lon2)
        )

    except (
        TypeError,
        ValueError
    ):

        return None

    earth_radius_km = 6371.0

    delta_lat = (
        lat2_rad - lat1_rad
    )

    delta_lon = (
        lon2_rad - lon1_rad
    )

    a = (
        math.sin(delta_lat / 2) ** 2
        +
        math.cos(lat1_rad)
        *
        math.cos(lat2_rad)
        *
        math.sin(delta_lon / 2) ** 2
    )

    # Protect against tiny floating point errors.
    a = max(
        0.0,
        min(1.0, a)
    )

    c = (
        2
        *
        math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )
    )

    kilometres = (
        earth_radius_km * c
    )

    return kilometres * 0.621371


# ============================================================
# LEEDS CORE POSTCODE TEST
#
# LS postcode district.
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
#
# Improved to reduce false positives.
# ============================================================

def name_looks_tree_related(name):

    if not name:
        return False

    text = str(name).lower()

    # Strong specific tree-sector terms.
    for pattern in TREE_STANDALONE_RELEVANT_PATTERNS:

        if re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        ):
            return True

    # Direct arboriculture/arborist/forestry terms.
    strong_terms = [
        "arborist",
        "arboriculture",
        "forestry",
        "tree surgeon",
        "tree surgery",
        "tree care",
        "tree felling",
        "tree removal",
        "stump grinding",
        "stump removal",
        "woodland"
    ]

    for term in strong_terms:

        if term in text:
            return True

    return False


# ============================================================
# SEARCH-TERM + COMPANY NAME CLASSIFICATION
#
# Some legitimate businesses may have names such as:
#
# "Smith Tree & Garden"
# "Smith Landscaping"
#
# Landscaping can therefore be relevant even without the word
# tree in the company name.
# ============================================================

def company_is_relevant(
    name,
    search_term
):

    if name_looks_tree_related(name):
        return True

    name_lower = (
        str(name or "")
        .lower()
        .strip()
    )

    search_lower = (
        str(search_term or "")
        .lower()
        .strip()
    )

    # A landscaping search can produce legitimate tree/grounds
    # businesses, but we don't want unrelated accountants etc.
    if search_lower == "landscaping":

        landscaping_terms = [
            "landscap",
            "garden",
            "grounds",
            "horticulture",
            "arbor",
            "tree",
            "hedge"
        ]

        return any(
            term in name_lower
            for term in landscaping_terms
        )

    # Forestry search.
    if search_lower == "forestry":

        return any(
            term in name_lower
            for term in [
                "forestry",
                "forest",
                "woodland",
                "tree",
                "arbor"
            ]
        )

    # Stump grinding.
    if search_lower == "stump grinding":

        return any(
            term in name_lower
            for term in [
                "stump",
                "tree",
                "arbor"
            ]
        )

    # All other searches require an actual relevant name.
    return name_looks_tree_related(name)


# ============================================================
# SERVICE AREA CLASSIFICATION
# ============================================================

def classify_service_area(distance):

    if distance is None:
        return "Unknown"

    if distance <= SERVICE_RADIUS_MILES:

        return (
            "Leeds Core"
            if distance <= SERVICE_RADIUS_MILES
            else "Leeds 15 Mile Service Area"
        )

    return "Outside Service Area"


# ============================================================
# SAVE COMPANY
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
                "https://find-and-update.company-information."
                "service.gov.uk/company/"
                + company_number
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

        "leeds_core_matches": 0,

        "within_15_mile_matches": 0,

        "outside_service_area": 0,

        "tree_named_companies": 0,

        "irrelevant_name_matches": 0,

        "new_partners_added": 0,

        "duplicates_skipped": 0,

        "api_errors": 0,

        "postcode_lookup_errors": 0,

        "sample_results": [],

        "sample_leeds_matches": []
    }

    session = get_ch_session()

    # Cache postcode coordinates.
    postcode_cache = {}

    # Avoid examining same company repeatedly during this run.
    seen_in_run = set()

    try:

        conn = psycopg2.connect(
            SURL
        )

        cur = conn.cursor()

        # ====================================================
        # SEARCH EACH TERM
        # ====================================================

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

                # ============================================
                # PROCESS COMPANIES
                # ============================================

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

                    # ----------------------------------------
                    # Tree-sector classification
                    # ----------------------------------------

                    tree_related = (
                        company_is_relevant(
                            name,
                            search_term
                        )
                    )

                    if tree_related:

                        stats[
                            "tree_named_companies"
                        ] += 1

                    else:

                        stats[
                            "irrelevant_name_matches"
                        ] += 1

                        # Don't waste postcode API calls on
                        # obvious unrelated Companies House
                        # results.
                        continue

                    address = company.get(
                        "address_snippet",
                        ""
                    )

                    postcode = extract_postcode(
                        address
                    )

                    if not postcode:
                        continue

                    stats[
                        "valid_postcodes"
                    ] += 1

                    # ========================================
                    # POSTCODE LOOKUP
                    # ========================================

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
                            or lon is None
                        ):

                            stats[
                                "postcode_lookup_errors"
                            ] += 1

                            continue

                        time.sleep(
                            0.05
                        )

                    # ========================================
                    # CRITICAL SAFETY CHECK
                    #
                    # This prevents the exact NoneType crash
                    # that appeared in your Render log.
                    # ========================================

                    if (
                        lat is None
                        or lon is None
                    ):

                        stats[
                            "postcode_lookup_errors"
                        ] += 1

                        continue

                    # ========================================
                    # DISTANCE FROM LEEDS
                    # ========================================

                    distance = distance_miles(
                        LEEDS_LAT,
                        LEEDS_LON,
                        lat,
                        lon
                    )

                    if distance is None:

                        stats[
                            "postcode_lookup_errors"
                        ] += 1

                        continue

                    distance = round(
                        distance,
                        2
                    )

                    # ========================================
                    # LEEDS CORE
                    # ========================================

                    is_core = (
                        is_leeds_core_postcode(
                            postcode
                        )
                    )

                    if is_core:

                        stats[
                            "leeds_core_matches"
                        ] += 1

                    # ========================================
                    # SERVICE AREA
                    #
                    # IMPORTANT:
                    # We use actual geographic distance.
                    #
                    # Therefore a postcode in BD/WF/HG/YO
                    # is included ONLY if its actual location
                    # is within 15 miles of Leeds.
                    #
                    # No manual postcode-district list needed.
                    # ========================================

                    if (
                        distance
                        <= SERVICE_RADIUS_MILES
                    ):

                        stats[
                            "within_15_mile_matches"
                        ] += 1

                        if is_core:

                            service_area = (
                                "Leeds Core"
                            )

                        else:

                            service_area = (
                                "Leeds 15 Mile "
                                "Service Area"
                            )

                        # ------------------------------------
                        # Only active companies become partners
                        # ------------------------------------

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
                                    "duplicates_skipped"
                                ] += 1

                        # ------------------------------------
                        # Leeds sample
                        # ------------------------------------

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

                    # ========================================
                    # GENERAL SAMPLE
                    # ========================================

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
                                (
                                    distance
                                    <= SERVICE_RADIUS_MILES
                                ),

                            "tree_related_name":
                                tree_related
                        })

                # Commit after each page.
                conn.commit()

                # ============================================
                # PAGINATION
                # ============================================

                start_index += (
                    ITEMS_PER_PAGE
                )

                if (
                    len(items)
                    <
                    ITEMS_PER_PAGE
                ):

                    break

                time.sleep(
                    REQUEST_DELAY
                )

        cur.close()
        conn.close()

        return stats

    except Exception as e:

        logger.exception(
            "Database save/discovery error"
        )

        return {
            "status": "error",
            "message": str(e),
            "stats": stats
        }


# ============================================================
# LEEDS COUNCIL PLANNING DATA
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

            "where":
                "1=1",

            "outFields":
                "*",

            "resultRecordCount":
                100,

            "orderByFields":
                "OBJECTID DESC",

            "f":
                "json"
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
# COUNCIL TREE CLASSIFICATION
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


def extract_council_description(record):

    # First look for likely planning-description fields.

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

    # Fallback: inspect all text fields.

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


def smart_classify(record):

    description = (
        extract_council_description(
            record
        )
    )

    if not description:

        return False, 0, ""

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
# LEEDS COUNCIL LEAD TEST
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

        lead = {

            "score":
                score,

            "summary":
                description,

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
        <title>Vector Data Labs</title>
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
            box-shadow:0 10px 30px rgba(0,0,0,0.1);
            border-top:6px solid #1b5e20;
            max-width:650px;
        ">

            <h1 style="color:#1b5e20;">
                Vector Data Labs
            </h1>

            <h2>
                V83.0 Leeds Discovery Master
            </h2>

            <div style="
                background:#f1f8e9;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
            ">

                <b>Core area:</b>
                Leeds LS postcodes<br>

                <b>Extended area:</b>
                15-mile geographic radius from Leeds<br>

                <b>Business source:</b>
                Companies House<br>

                <b>Planning source:</b>
                Leeds City Council

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
                Run Leeds Business Research
            </a>

            <br><br>

            <a href="/test-regional"
               style="color:#555;">
                Check Leeds Council Leads
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
# BUSINESS DISCOVERY ROUTE
# ============================================================

@app.get(
    "/research-leeds"
)
def run_discovery():

    return discover_leeds_partners()


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
            SERVICE_RADIUS_MILES
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
