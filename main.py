def distance_miles(lat1, lon1, lat2, lon2):
    """
    Safely calculate distance between two coordinates.

    Returns None when either coordinate is missing or invalid.
    This prevents one bad postcode from crashing the entire research run.
    """
    try:
        if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
            return None

        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)

        if not all(math.isfinite(v) for v in [lat1, lon1, lat2, lon2]):
            return None

        radius = 3958.8

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)

        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)

        a = (
            math.sin(d_phi / 2) ** 2
            + math.cos(phi1)
            * math.cos(phi2)
            * math.sin(d_lambda / 2) ** 2
        )

        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return radius * c

    except (TypeError, ValueError, OverflowError):
        return None
