"""Framework-independent logic shared by the Flask app and the FastAPI service.

Anything here must behave byte-identically in both stacks — it is what lets the
client-facing contract stay frozen while the backend is replaced.
"""

import re
from datetime import datetime, timedelta, timezone

MIN_SIGNATURE_FIELDS = 2

SIGNATURE_FIELDS = ("mac_address", "motherboard_serial", "machine_guid", "bios_serial")

NUMBER_WORD_TO_DIGIT = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

OTP_CHANNEL = "orbitalcore:otp"


def norm_sig(value):
    if value is None:
        return ""
    # Fingerprints are retyped by hand in the admin panel and re-reported by
    # each PC, so the same identifier arrives with different casing depending
    # on who typed it. Matching was byte-exact, so a lowercase MAC or an
    # uppercase GUID failed to match the stored value and the device was
    # rejected even though it was registered. Compare case-insensitively.
    return str(value).strip().upper()


def normalize_device_signatures(data):
    """Returns (mac, motherboard, guid, bios, non_empty_count)."""
    values = [norm_sig(data.get(name)) for name in SIGNATURE_FIELDS]
    return (*values, sum(1 for v in values if v))


def normalize_bd_phone(value):
    if value is None:
        return ""
    s = re.sub(r"[\s\-()]+", "", str(value).strip())
    if not s:
        return ""
    if s.startswith("+"):
        s = s[1:]
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return ""
    d = digits
    if len(d) >= 12 and d.startswith("880") and len(d) > 3 and d[3] == "1":
        d = d[3:]
    if len(d) == 10 and d[0] == "1":
        d = "0" + d
    return d


def extract_normalized_otp(raw):
    """Turn IVAC word OTPs / spaced digits into a digit string."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    tokens = re.findall(r"[a-z]+", text.lower())
    best = []
    current = []
    for tok in tokens:
        if tok in NUMBER_WORD_TO_DIGIT:
            current.append(NUMBER_WORD_TO_DIGIT[tok])
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    if len(best) >= 4:
        return "".join(best)

    spaced = re.findall(r"(?:\d[\s\-]+){3,}\d", text)
    if spaced:
        return re.sub(r"\D", "", spaced[-1])

    blocks = re.findall(r"\d{4,8}", text)
    if blocks:
        return blocks[-1]
    runs = re.findall(r"\d+", text)
    if runs:
        return max(runs, key=len)

    return ""


def format_bdt(dt=None):
    bdt_tz = timezone(timedelta(hours=6))
    if dt is None:
        now = datetime.now(bdt_tz)
    elif dt.tzinfo is None:
        now = dt.replace(tzinfo=timezone.utc).astimezone(bdt_tz)
    else:
        now = dt.astimezone(bdt_tz)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+06:00"


def parse_subscription(raw_message):
    """Returns (set_of_phones, error_reason). Mirrors the Flask behaviour exactly."""
    import json

    try:
        data = json.loads(raw_message)
    except (TypeError, ValueError):
        return None, "Invalid JSON"

    if not isinstance(data, dict) or data.get("action") != "subscribe":
        return None, "Expected a subscribe action"

    raw_phones = data.get("phones")
    if not isinstance(raw_phones, list) or not raw_phones:
        return None, "phones must be a non-empty array"

    phones = set()
    for raw_phone in raw_phones:
        if not isinstance(raw_phone, str):
            return None, "Each phone must be a string"
        phone = normalize_bd_phone(raw_phone)
        if not phone:
            return None, "Each phone must contain digits"
        phones.add(phone)

    return phones, None


def match_signatures(records, mac, mb, guid, bios):
    """2-of-4 fingerprint match.

    `records` is an iterable of objects exposing the four signature attributes.
    Returns (record, error) where error is None, "insufficient", "ambiguous"
    or "not_found".
    """
    provided = {
        "mac_address": mac,
        "motherboard_serial": mb,
        "machine_guid": guid,
        "bios_serial": bios,
    }
    provided = {name: value for name, value in provided.items() if value}
    if len(provided) < MIN_SIGNATURE_FIELDS:
        return None, "insufficient"

    matches = []
    for record in records:
        agreed = sum(
            1
            for name, value in provided.items()
            if norm_sig(getattr(record, name)) == value
        )
        if agreed >= MIN_SIGNATURE_FIELDS:
            matches.append(record)

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"