"""Storage-independent date parsing shared by discovery and mirror reconciliation."""

import re
from datetime import datetime

def parse_listing_date(date_str: str, year: str = "") -> datetime | None:
    """Parse a date string from the circular listing table."""
    date_str = date_str.strip()
    if not date_str:
        return None

    # Append the year only if no year already appears in the date string.
    if year and not re.search(r"\b(?:19|20)\d{2}\b", date_str):
        date_str = f"{date_str}, {year}"

    clean_date_str = re.sub(r'(?<=\d)(st|nd|rd|th)', '', date_str) #14th, 2nd etc
    clean_date_str = re.sub(r"\s+,\s*", ", ", clean_date_str)   # "January 15 , 2025" -> "January 15, 2025"
    clean_date_str = re.sub(r"\s+", " ", clean_date_str)        # "January  15, 2025" -> "January 15, 2025"

    formats = [
        "%b %d, %Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d %B, %Y",
        "%d %B %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(clean_date_str, fmt)
        except ValueError:
            continue

    return None
