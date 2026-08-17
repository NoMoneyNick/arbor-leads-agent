import os
import logging
import requests
import psycopg2
import urllib3
import re
import time
import math

from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse

# ============================================================
# VECTOR DATA LABS
# V82.0 - LEEDS + 15 MILE SERVICE AREA
# ============================================================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="Vector Data Labs - V82 Leeds Lead Discovery",
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


# ============================================================
# API SETTINGS
# ============================================================

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk"
)

POSTCODES_IO_URL = "https://api.postcodes.io"

LEEDS_COUNCIL_URL = (
    "https://mapservices.leeds.gov.uk/"
    "arcgis/rest/services/Public/Planning/MapServer/12/query"
)

# Leeds city-centre reference point.
# Used only for calculating the commercial service radius.
LEEDS_LAT = 53.8008
LEEDS_LON = -1.5491

# Commercial service radius.
SERVICE_RADIUS_MILES = 15.0

# Companies House search settings.
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

# Be polite to external services.
REQUEST_DELAY = 0.25

# Postcodes.io allows batch postcode lookups.
POSTCODE_BATCH_SIZE = 100


# ============================================================
# LEEDS POSTCODE CORE
# ============================================================

# The LS postcode area contains geographic districts plus
# non-geographic LS88, LS98 and LS99.
#
# We treat geographic LS districts as the Leeds CORE.
# Non-geographic LS districts are deliberately excluded.

LS_CORE_DISTRICTS = {
    "LS1", "LS2", "LS3", "LS4", "LS5", "LS6", "LS7", "LS8",
    "LS9", "LS10", "LS11", "LS12", "LS13", "LS14", "LS15",
    "LS16", "LS17", "LS18", "LS19", "LS20", "LS21", "LS22",
    "LS23", "LS24", "LS25", "LS26", "LS27", "LS28", "LS29"
}

NON_GEOGRAPHIC_LS = {
    "LS88", "LS98", "LS99"
}


# ============================================================
# TREE DISCOVERY TERMS
# ============================================================

TREE_NAME_TERMS = [
    "tree",
    "trees",
    "tree surgeon",
    "tree surgery",
    "tree service",
    "tree services",
    "tree care",
    "arborist",
    "arboriculture",
    "forestry",
    "stump",
    "stump grinding",
    "woodland",
    "grounds",
    "landscap"
]


# ============================================================
# COUNCIL CLASSIFICATION
# ============================================================

TREE_GOLD = [
    "tree",
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
    "conifer",
    "woodland"
]

COUNCIL_DESCRIPTION_HEADERS = [
    "proposal",
    "description",
    "development_description",
    "nature",
    "details",
    "development",
    "application_description",
    "applicationdetails",
    "siteAddress",
    "address"
]


# ============================================================
# DATABASE SETUP
# ============================================================

def get_db_connection():
    if not SURL:
        raise RuntimeError("SUPABASE_DB_URL missing")

    return psycopg2.connect(SURL)


def init_db():
    if not SURL:
        logger.warning("SUPABASE_DB_URL missing - database disabled")
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Main company discovery table.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS potential_partners (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                company_name TEXT,
                company_number TEXT UNIQUE,

                status TEXT,

                date_incorporated DATE,

                address TEXT,
                postcode TEXT,

                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,

                distance_from_leeds_miles DOUBLE PRECISION,

                service_area TEXT,

                operational_confidence INT DEFAULT 0,

                sic_codes TEXT[],

                discovery_source TEXT,

                companies_house_url TEXT,

                last_verified TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Council lead table.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS council_tree_leads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

                council TEXT,

                application_reference TEXT,

                received_date DATE,

                decision_date DATE,

                status TEXT,

                address TEXT,

                postcode TEXT,

                summary TEXT,

                full_description TEXT,

                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,

                distance_from_leeds_miles DOUBLE PRECISION,

                service_area TEXT,

                source_url TEXT,

                source TEXT,

                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),

                UNIQUE(council, application_reference)
            );
        """)

        conn.commit()
        cur.close()
        conn.close()

        logger.info("Database schema verified.")

    except Exception as e:
        logger.error(f"Database schema error: {e}")


init_db()


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def normalize_postcode(postcode):
    if not postcode:
        return None

    postcode = str(postcode).upper().strip()

    postcode = re.sub(
        r"\s+",
        "",
        postcode
    )

    # Basic UK postcode validation.
    pattern = r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$"

    if not re.match(pattern, postcode):
        return None

    return postcode


def postcode_district(postcode):
    postcode = normalize_postcode(postcode)

    if not postcode:
        return None

    match = re.match(
        r"^([A-Z]{1,2}\d{1,2})",
        postcode
    )

    if not match:
        return None

    return match.group(1)


def haversine_miles(lat1, lon1, lat2, lon2):
    """
    Calculate great-circle distance in miles.
    """

    radius_miles = 3958.7613

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        +
        math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )

    return radius_miles * c


# ============================================================
# POSTCODE DISTANCE ENGINE
# ============================================================

def classify_postcode(postcode):
    """
    Returns basic geographical classification before
    coordinate verification.

    LS geographic districts are always considered CORE.

    Non-LS districts must subsequently pass the
    15-mile distance test.
    """

    postcode = normalize_postcode(postcode)

    if not postcode:
        return {
            "valid": False,
            "district": None,
            "area": None,
            "service_area": None
        }

    district = postcode_district(postcode)

    if not district:
        return {
            "valid": False,
            "district": None,
            "area": None,
            "service_area": None
        }

    area_match = re.match(
        r"^([A-Z]{1,2})",
        district
    )

    area = area_match.group(1) if area_match else None

    if district in LS_CORE_DISTRICTS:
        return {
            "valid": True,
            "district": district,
            "area": area,
            "service_area": "Leeds Core"
        }

    if district in NON_GEOGRAPHIC_LS:
        return {
            "valid": False,
            "district": district,
            "area": area,
            "service_area": None
        }

    return {
        "valid": True,
        "district": district,
        "area": area,
        "service_area": "Distance Verified"
    }


def batch_postcode_coordinates(postcodes):
    """
    Batch postcode lookups through Postcodes.io.

    This is much better than making one HTTP request for
    every company individually.
    """

    unique = []

    for postcode in postcodes:
        postcode = normalize_postcode(postcode)

        if postcode and postcode not in unique:
            unique.append(postcode)

    results = {}

    if not unique:
        return results

    session = requests.Session()

    for start in range(
        0,
        len(unique),
        POSTCODE_BATCH_SIZE
    ):

        batch = unique[
            start:start + POSTCODE_BATCH_SIZE
        ]

        try:
            response = session.post(
                f"{POSTCODES_IO_URL}/postcodes",
                json={
                    "postcodes": batch
                },
                timeout=30
            )

            if response.status_code != 200:
                logger.warning(
                    f"Postcodes.io error: "
                    f"{response.status_code}"
                )

                continue

            data = response.json()

            for result in data.get(
                "result",
                []
            ):

                query = normalize_postcode(
                    result.get("query")
                )

                if not query:
                    continue

                if result.get("result") is None:
                    continue

                geo = result["result"]

                lat = geo.get("latitude")
                lon = geo.get("longitude")

                if lat is None or lon is None:
                    continue

                results[query] = {
                    "latitude": float(lat),
                    "longitude": float(lon)
                }

        except Exception as e:
            logger.warning(
                f"Postcode lookup error: {e}"
            )

        time.sleep(REQUEST_DELAY)

    return results


def calculate_service_area(postcode, coordinates):
    """
    Decide whether a postcode is commercially usable.

    LS geographic postcodes = Leeds Core.

    Everything outside LS must actually be within
    SERVICE_RADIUS_MILES of Leeds.
    """

    info = classify_postcode(postcode)

    if not info["valid"]:
        return {
            "accepted": False,
            "service_area": None,
            "distance_miles": None
        }

    # All geographic LS districts are accepted as the
    # Leeds core.
    if info["service_area"] == "Leeds Core":

        if not coordinates:
            return {
                "accepted": True,
                "service_area": "Leeds Core",
                "distance_miles": None
            }

        distance = haversine_miles(
            LEEDS_LAT,
            LEEDS_LON,
            coordinates["latitude"],
            coordinates["longitude"]
        )

        return {
            "accepted": True,
            "service_area": "Leeds Core",
            "distance_miles": round(distance, 2)
        }

    # Non-LS postcodes require an actual coordinate check.
    if not coordinates:
        return {
            "accepted": False,
            "service_area": None,
            "distance_miles": None
        }

    distance = haversine_miles(
        LEEDS_LAT,
        LEEDS_LON,
        coordinates["latitude"],
        coordinates["longitude"]
    )

    if distance <= SERVICE_RADIUS_MILES:
        return {
            "accepted": True,
            "service_area": "Leeds 15 Mile Radius",
            "distance_miles": round(distance, 2)
        }

    return {
        "accepted": False,
        "service_area": None,
        "distance_miles": round(distance, 2)
    }


# ============================================================
# TREE BUSINESS CLASSIFICATION
# ============================================================

def name_looks_tree_related(name):
    if not name:
        return False

    name = clean_text(name).lower()

    return any(
        term in name
        for term in TREE_NAME_TERMS
    )


def calculate_company_confidence(
    company_name,
    address,
    service_area,
    status
):
    score = 0

    if status == "active":
        score += 30

    if service_area == "Leeds Core":
        score += 25
    elif service_area == "Leeds 15 Mile Radius":
        score += 20

    if name_looks_tree_related(company_name):
        score += 35

    address_lower = clean_text(
        address
    ).lower()

    if any(
        term in address_lower
        for term in [
            "tree",
            "arbor",
            "forestry",
            "stump",
            "landscape"
        ]
    ):
        score += 10

    return min(score, 100)


# ============================================================
# COMPANIES HOUSE SESSION
# ============================================================

def get_ch_session():

    if not CH_KEY:
        raise RuntimeError(
            "COMPANIES_HOUSE_KEY missing"
        )

    session = requests.Session()

    # Companies House uses the API key as the username
    # with an empty password.
    session.auth = (
        CH_KEY,
        ""
    )

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/82.0 "
            "(Leeds tree-services discovery)"
    })

    return session


# ============================================================
# COMPANIES HOUSE DISCOVERY
# ============================================================

def discover_leeds_partners():

    if not CH_KEY:
        return {
            "status": "error",
            "message": "COMPANIES_HOUSE_KEY missing"
        }

    if not SURL:
        return {
            "status": "error",
            "message": "SUPABASE_DB_URL missing"
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
        "new_partners_added": 0,
        "duplicates_skipped": 0,
        "api_errors": 0,
        "postcode_lookup_errors": 0,
        "sample_results": [],
        "sample_leeds_matches": []
    }

    session = get_ch_session()

    seen_in_run = set()
    companies_to_process = []

    # --------------------------------------------------------
    # STEP 1
    # Search Companies House and collect candidates.
    # --------------------------------------------------------

    for term in SEARCH_TERMS:

        stats["search_terms"] += 1

        logger.info(
            f"Searching Companies House for: {term}"
        )

        start_index = 0

        while start_index < MAX_RESULTS_PER_TERM:

            try:

                response = session.get(
                    f"{COMPANIES_HOUSE_URL}/search/companies",
                    params={
                        "q": term,
                        "items_per_page": ITEMS_PER_PAGE,
                        "start_index": start_index
                    },
                    timeout=20
                )

            except Exception as e:

                stats["api_errors"] += 1

                logger.error(
                    f"Companies House request error "
                    f"for '{term}': {e}"
                )

                break

            if response.status_code != 200:

                stats["api_errors"] += 1

                logger.error(
                    f"Companies House returned "
                    f"{response.status_code} "
                    f"for '{term}'"
                )

                break

            try:
                data = response.json()

            except Exception:

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

                stats["companies_examined"] += 1

                company_number = company.get(
                    "company_number"
                )

                if not company_number:
                    continue

                # Avoid processing the same company repeatedly
                # when it appears under multiple search terms.
                if company_number in seen_in_run:
                    continue

                seen_in_run.add(
                    company_number
                )

                status = company.get(
                    "company_status"
                )

                if status == "active":
                    stats["active_companies"] += 1
                else:
                    stats["inactive_companies"] += 1

                company_name = company.get(
                    "title",
                    ""
                )

                if name_looks_tree_related(
                    company_name
                ):
                    stats["tree_named_companies"] += 1

                address = company.get(
                    "address_snippet",
                    ""
                )

                postcode = None

                address_object = company.get(
                    "address"
                )

                if isinstance(
                    address_object,
                    dict
                ):
                    postcode = address_object.get(
                        "postal_code"
                    )

                if not postcode:
                    # Fallback: find postcode in address text.
                    postcode_match = re.search(
                        r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b",
                        address.upper()
                    )

                    if postcode_match:
                        postcode = postcode_match.group(
                            0
                        )

                postcode = normalize_postcode(
                    postcode
                )

                if postcode:
                    stats["valid_postcodes"] += 1

                companies_to_process.append({
                    "search_term": term,
                    "company_number": company_number,
                    "company_name": company_name,
                    "status": status,
                    "address": address,
                    "postcode": postcode,
                    "date_of_creation":
                        company.get(
                            "date_of_creation"
                        ),
                    "company": company
                })

            # Companies House supports start_index
            # for pagination.
            start_index += ITEMS_PER_PAGE

            if len(items) < ITEMS_PER_PAGE:
                break

            time.sleep(
                REQUEST_DELAY
            )

    # --------------------------------------------------------
    # STEP 2
    # Batch postcode coordinates.
    # --------------------------------------------------------

    postcodes = [
        company["postcode"]
        for company in companies_to_process
        if company["postcode"]
    ]

    coordinates = batch_postcode_coordinates(
        postcodes
    )

    # --------------------------------------------------------
    # STEP 3
    # Apply geographic filter.
    # --------------------------------------------------------

    accepted_candidates = []

    for company in companies_to_process:

        postcode = company["postcode"]

        if not postcode:
            continue

        geo = coordinates.get(
            postcode
        )

        geographic_result = calculate_service_area(
            postcode,
            geo
        )

        company["latitude"] = (
            geo["latitude"]
            if geo
            else None
        )

        company["longitude"] = (
            geo["longitude"]
            if geo
            else None
        )

        company["distance_miles"] = (
            geographic_result[
                "distance_miles"
            ]
        )

        company["service_area"] = (
            geographic_result[
                "service_area"
            ]
        )

        company["accepted"] = (
            geographic_result[
                "accepted"
            ]
        )

        if not company["accepted"]:
            stats["outside_service_area"] += 1
            continue

        if company["service_area"] == "Leeds Core":
            stats["leeds_core_matches"] += 1

        elif company["service_area"] == "Leeds 15 Mile Radius":
            stats["within_15_mile_matches"] += 1

        accepted_candidates.append(
            company
        )

    # --------------------------------------------------------
    # STEP 4
    # Save accepted active companies.
    # --------------------------------------------------------

    conn = None
    cur = None

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        for company in accepted_candidates:

            if company["status"] != "active":
                continue

            company_name = company[
                "company_name"
            ]

            company_number = company[
                "company_number"
            ]

            address = company[
                "address"
            ]

            postcode = company[
                "postcode"
            ]

            confidence = calculate_company_confidence(
                company_name,
                address,
                company["service_area"],
                company["status"]
            )

            # Sample results for debugging.
            if len(stats["sample_results"]) < 20:

                stats["sample_results"].append({
                    "company_name":
                        company_name,

                    "company_number":
                        company_number,

                    "status":
                        company["status"],

                    "address":
                        address,

                    "postcode":
                        postcode,

                    "distance_from_leeds_miles":
                        company["distance_miles"],

                    "service_area":
                        company["service_area"],

                    "tree_related_name":
                        name_looks_tree_related(
                            company_name
                        )
                })

            cur.execute(
                """
                SELECT 1
                FROM potential_partners
                WHERE company_number = %s
                """,
                (company_number,)
            )

            if cur.fetchone():

                stats["duplicates_skipped"] += 1
                continue

            companies_house_url = (
                "https://find-and-update.company-information.service.gov.uk/company/"
                + company_number
            )

            cur.execute(
                """
                INSERT INTO potential_partners (
                    company_name,
                    company_number,
                    status,
                    address,
                    postcode,
                    latitude,
                    longitude,
                    distance_from_leeds_miles,
                    service_area,
                    date_incorporated,
                    operational_confidence,
                    discovery_source,
                    companies_house_url,
                    last_verified,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, NOW(), NOW()
                )
                """,
                (
                    company_name,
                    company_number,
                    company["status"],
                    address,
                    postcode,
                    company["latitude"],
                    company["longitude"],
                    company["distance_miles"],
                    company["service_area"],
                    company["date_of_creation"],
                    confidence,
                    "Companies House Leeds 15 Mile Discovery",
                    companies_house_url
                )
            )

            stats["new_partners_added"] += 1

            if (
                len(
                    stats["sample_leeds_matches"]
                ) < 20
            ):

                stats[
                    "sample_leeds_matches"
                ].append({
                    "company_name":
                        company_name,

                    "company_number":
                        company_number,

                    "status":
                        company["status"],

                    "address":
                        address,

                    "postcode":
                        postcode,

                    "distance_from_leeds_miles":
                        company["distance_miles"],

                    "service_area":
                        company["service_area"],

                    "tree_related_name":
                        name_looks_tree_related(
                            company_name
                        )
                })

        conn.commit()

    except Exception as e:

        if conn:
            conn.rollback()

        logger.error(
            f"Database save error: {e}"
        )

        return {
            "status": "error",
            "message": str(e),
            "stats": stats
        }

    finally:

        if cur:
            cur.close()

        if conn:
            conn.close()

    return stats


# ============================================================
# COUNCIL HELPERS
# ============================================================

def extract_council_text(record):
    """
    Find the most useful descriptive field in a Leeds
    Council planning record.
    """

    # First preference: known descriptive fields.
    preferred_keys = [
        "PROPOSAL",
        "Proposal",
        "proposal",
        "DESCRIPTION",
        "Description",
        "description",
        "DEVELOPMENT_DESCRIPTION",
        "development_description",
        "Nature",
        "NATURE",
        "details",
        "DETAILS"
    ]

    for key in preferred_keys:

        value = record.get(key)

        if value:
            text = clean_text(value)

            if len(text) > 5:
                return text

    # Fallback: inspect all fields.
    for key, value in record.items():

        if not value:
            continue

        key_lower = key.lower()

        if any(
            word in key_lower
            for word in [
                "proposal",
                "description",
                "development",
                "nature",
                "detail"
            ]
        ):

            text = clean_text(value)

            if len(text) > 5:
                return text

    return ""


def extract_council_address(record):
    """
    Pull the best available address from the council
    record.
    """

    preferred_keys = [
        "siteAddress",
        "site_address",
        "SiteAddress",
        "address",
        "Address",
        "ADDRESS",
        "SITEADDRESS"
    ]

    for key in preferred_keys:

        value = record.get(key)

        if value:
            return clean_text(value)

    # Fallback: search keys containing address.
    for key, value in record.items():

        if value and "address" in key.lower():
            return clean_text(value)

    return ""


def extract_application_reference(record):
    keys = [
        "reference",
        "Reference",
        "application_reference",
        "APPLICATION_REFERENCE",
        "app_no",
        "AppNo",
        "application_number",
        "ApplicationNumber"
    ]

    for key in keys:

        value = record.get(key)

        if value:
            return clean_text(value)

    return ""


def extract_record_date(record):
    keys = [
        "received_date",
        "ReceivedDate",
        "date_received",
        "DateReceived",
        "received",
        "Received"
    ]

    for key in keys:

        value = record.get(key)

        if value:
            return clean_text(value)

    return ""


def smart_classify(record):

    description = extract_council_text(
        record
    ).lower()

    if not description:
        return False, 0, ""

    tree_matches = [
        word
        for word in TREE_GOLD
        if word in description
    ]

    if not tree_matches:
        return False, 0, description

    score = len(
        tree_matches
    ) * 5

    if any(
        word in description
        for word in [
            "fell",
            "felling",
            "tpo",
            "remove",
            "removal",
            "prune",
            "pruning",
            "crown"
        ]
    ):
        score += 15

    return (
        score >= 10,
        score,
        description
    )


def create_lead_summary(description):
    """
    Convert the council's proposal into a clean summary
    for humans and downstream lead selling.
    """

    description = clean_text(
        description
    )

    if not description:
        return "Tree-related planning application"

    # Avoid returning enormous council descriptions.
    if len(description) > 350:
        description = description[:347] + "..."

    return description


# ============================================================
# LEEDS COUNCIL DATA ENGINE
# ============================================================

def fetch_council():

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "VectorDataLabs/82.0",
        "Referer":
            "https://www.leeds.gov.uk/"
    })

    try:

        params = {
            "where": "1=1",
            "outFields": "*",
            "resultRecordCount": 200,
            "orderByFields": "OBJECTID DESC",
            "f": "json"
        }

        response = session.get(
            LEEDS_COUNCIL_URL,
            params=params,
            timeout=30,
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
            f"Leeds Council error: {e}"
        )

        return [], "Fault"


# ============================================================
# COUNCIL TREE LEAD PROCESSOR
# ============================================================

def process_council_leads():

    records, status = fetch_council()

    if status != "Online":

        return {
            "status": status,
            "records_downloaded": 0,
            "tree_related_records": 0,
            "tree_applications": []
        }

    candidates = []

    for record in records:

        is_tree, score, description = (
            smart_classify(record)
        )

        if not is_tree:
            continue

        address = extract_council_address(
            record
        )

        reference = (
            extract_application_reference(
                record
            )
        )

        received_date = (
            extract_record_date(
                record
            )
        )

        summary = create_lead_summary(
            description
        )

        candidates.append({
            "application_reference":
                reference,

            "received_date":
                received_date,

            "address":
                address,

            "summary":
                summary,

            "score":
                score,

            "raw_record":
                record
        })

    return {
        "status": status,
        "records_downloaded": len(records),
        "tree_related_records": len(candidates),
        "tree_applications": candidates
    }


# ============================================================
# DATABASE SAVE FOR COUNCIL LEADS
# ============================================================

def save_council_leads(leads):

    if not SURL:
        return 0

    saved = 0

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        for lead in leads:

            reference = (
                lead["application_reference"]
            )

            if not reference:
                # We need a stable identifier for
                # duplicate protection.
                continue

            cur.execute(
                """
                SELECT 1
                FROM council_tree_leads
                WHERE council = %s
                AND application_reference = %s
                """,
                (
                    "Leeds",
                    reference
                )
            )

            if cur.fetchone():
                continue

            cur.execute(
                """
                INSERT INTO council_tree_leads (
                    council,
                    application_reference,
                    received_date,
                    address,
                    summary,
                    full_description,
                    source,
                    updated_at
                )
                VALUES (
                    %s, %s, NULL, %s,
                    %s, %s,
                    %s, NOW()
                )
                """,
                (
                    "Leeds",
                    reference,
                    lead["address"],
                    lead["summary"],
                    lead["summary"],
                    "Leeds City Council"
                )
            )

            saved += 1

        conn.commit()

        cur.close()
        conn.close()

    except Exception as e:

        logger.error(
            f"Council lead database error: {e}"
        )

    return saved


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/")
def lander():

    return """
    <html>

    <head>
        <title>Vector Data Labs</title>
    </head>

    <body style="
        font-family: Arial, sans-serif;
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
                V82 Leeds Lead Discovery
            </h2>

            <div style="
                background:#f1f8e9;
                padding:15px;
                border-radius:8px;
                margin:20px 0;
                text-align:left;
                font-size:14px;
            ">

                <b>Geographic strategy</b><br><br>

                Leeds LS postcode districts =
                <b>Leeds Core</b><br>

                Non-LS postcode =
                <b>maximum 15 miles from Leeds</b><br><br>

                Companies House =
                <b>Paginated discovery</b><br>

                Council =
                <b>Tree-work classification</b>

            </div>

            <a href="/research-leeds"
               style="
                   display:inline-block;
                   padding:14px 28px;
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
               style="
                   color:#555;
               ">
                Test Leeds Council Leads
            </a>

            <br><br>

            <a href="/docs"
               style="
                   color:#777;
               ">
                API Documentation
            </a>

        </div>

    </body>
    </html>
    """


# ============================================================
# COMPANIES HOUSE ROUTE
# ============================================================

@app.get("/research-leeds")
def run_discovery():

    return discover_leeds_partners()


# ============================================================
# COUNCIL TEST ROUTE
# ============================================================

@app.get("/test-regional")
def test_leeds():

    result = process_council_leads()

    saved = save_council_leads(
        result["tree_applications"]
    )

    result["new_database_leads"] = saved

    return result


# ============================================================
# FULL TEST ROUTE
# ============================================================

@app.get("/test-all")
def test_all():

    company_result = discover_leeds_partners()

    council_result = process_council_leads()

    council_saved = save_council_leads(
        council_result["tree_applications"]
    )

    return {
        "companies_house":
            company_result,

        "leeds_council":
            council_result,

        "new_council_database_leads":
            council_saved
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "version": "V82.0",
        "service_area": "Leeds Core + 15 mile external radius",
        "companies_house":
            bool(CH_KEY),
        "database":
            bool(SURL),
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat()
    }


# ============================================================
# OPTIONAL PROTECTED TRIGGER
# ============================================================

@app.get("/trigger-scrape")
def trigger_scrape(
    x_trigger_secret: str = Header(
        default=""
    )
):

    if T_SEC and x_trigger_secret != T_SEC:
        raise HTTPException(
            status_code=403,
            detail="Invalid trigger secret"
        )

    council_result = (
        process_council_leads()
    )

    saved = save_council_leads(
        council_result[
            "tree_applications"
        ]
    )

    return {
        "status": "success",

        "council":
            "Leeds",

        "records_downloaded":
            council_result[
                "records_downloaded"
            ],

        "tree_related_records":
            council_result[
                "tree_related_records"
            ],

        "new_leads_saved":
            saved,

        "leads":
            council_result[
                "tree_applications"
            ]
    }
