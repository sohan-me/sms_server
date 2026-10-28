"""Turn one SMS into a channel decision, a code and a sender label.

The server used to treat every inbound message as a single OTP stream: it
scanned the body for a digit run and pushed it to every subscribed client.
That cannot serve three channels, and it hard-codes one IVAC format.

Three properties are deliberate here:

* extraction never depends on who sent the message — IVAC changing its sender
  number must not change routing;
* classification never gates on a sender allowlist — the body decides, and the
  sender is only a hint for the label and for brands the body omits;
* the patterns are data (`sms_patterns.json`), loaded by mtime, so IVAC moving
  from ``ONE-TWO-THREE-FOUR-FIVE-SIX`` to ``ONE_TWO_THREE_FOUR_FIVE_SIX`` and
  the next change after that are file edits, not code changes.

Framework-independent on purpose, like ``otp_core``: it is imported by the
server, by the ingest path and by the tests without a Flask app in sight.
"""

import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple

from otp_core import NUMBER_WORD_TO_DIGIT

logger = logging.getLogger(__name__)

KIND_SIGNIN = "signin"
KIND_PAYMENT = "payment"
KIND_TRANSACTION = "transaction"
KIND_UNKNOWN = "unknown"

# The three kinds that have a public endpoint and a WebSocket channel.
DELIVERY_KINDS = (KIND_SIGNIN, KIND_PAYMENT, KIND_TRANSACTION)

DEFAULT_PATTERNS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sms_patterns.json"
)

# Bengali digits appear in some operator templates; normalise them before any
# pattern runs so one rule covers both scripts.
_BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# The separators providers actually alternate between. IVAC's format moved
# from "-" to "_", so both (and whitespace) must be equivalent.
_SEPARATOR_CHARS = r"\s\-_.·•"
_SEP_ONLY_RE = re.compile(r"[\s\-_.·•]*")
_TOKEN_RE = re.compile(r"[a-z]+|\d")
# The trailing lookahead has to span separators: without it "12 34 56"
# matches only "12 34" and the span looks like a two-group non-code.
_SEPARATED_SPAN_RE = re.compile(
    r"(?<!\d)\d+(?:[" + _SEPARATOR_CHARS + r"]+\d+)+(?![" + _SEPARATOR_CHARS + r"]*\d)"
)
_DIGIT_GROUP_SPLIT_RE = re.compile(r"[" + _SEPARATOR_CHARS + r"]+")
# Word boundaries matter: without them an 11-digit phone number would be
# truncated to 8 digits and read as a code.
_CONTIGUOUS_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_TRX_TOKEN_RE = re.compile(r"[A-Za-z0-9]{6,20}")
_AMOUNT_RE = re.compile(
    r"(?:(?:bdt|tk|taka|৳)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:bdt|tk|taka|৳))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedSms:
    """One message's routing decision.

    ``otp`` is only set for sign-in and payment OTPs, ``trx``/``amount`` only
    for a completed transaction, so a consumer can never mistake an amount for
    a code. ``code`` is whichever of the two applies.
    """

    kind: str = KIND_UNKNOWN
    code: str = ""
    otp: str = ""
    trx: str = ""
    amount: str = ""
    sender: str = ""
    sender_number: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Brand:
    label: str
    family: str
    pattern: "re.Pattern"


@dataclass(frozen=True)
class SmsPatterns:
    brands: Tuple[Brand, ...]
    keywords: Dict[str, Tuple["re.Pattern", ...]]
    trx_anchor: "re.Pattern"
    senders: Dict[str, Dict[str, str]]
    source: str = ""


def normalize_text(raw) -> str:
    """NFKC + Bengali digits, but keep case: TRX IDs are case-bearing."""
    return unicodedata.normalize("NFKC", str(raw or "")).translate(_BENGALI_DIGITS).strip()


def _phrase_pattern(phrase: str) -> "re.Pattern":
    # Boundaries instead of plain substrings, so "otp" cannot match "otpnot".
    # Digits are allowed on either side on purpose: "OTP123456" and
    # "IVAC_OTP_123456" are common no-separator templates.
    return re.compile(
        r"(?<![a-z])" + re.escape(phrase.lower()) + r"(?![a-z])"
    )


def _alternation(phrases: Iterable[str], flags: int = 0) -> "re.Pattern":
    # Longest first: at one position "transaction id" must win over "trx", and
    # "trx id" over "trx".
    ordered = sorted({p.strip().lower() for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not ordered:
        raise ValueError("a pattern list cannot be empty")
    return re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(p) for p in ordered) + r")(?![a-z0-9])",
        flags,
    )


def build_patterns(data: dict, source: str = "") -> SmsPatterns:
    """Validate a patterns document and compile it once.

    Validation is strict on purpose: a typo in this file silently misroutes
    OTPs, which is far worse than failing to boot.
    """
    if not isinstance(data, dict):
        raise ValueError("SMS patterns must be a JSON object")

    raw_brands = data.get("brands")
    if not isinstance(raw_brands, list) or not raw_brands:
        raise ValueError("SMS patterns need a non-empty 'brands' list")

    brands = []
    for entry in raw_brands:
        if not isinstance(entry, dict):
            raise ValueError("each brand must be an object")
        label = str(entry.get("label") or "").strip()
        family = str(entry.get("family") or "").strip()
        aliases = entry.get("aliases")
        if not label:
            raise ValueError("every brand needs a 'label'")
        if family not in DELIVERY_KINDS:
            raise ValueError(
                f"brand {label!r} has family {family!r}; expected one of {DELIVERY_KINDS}"
            )
        if not isinstance(aliases, list) or not any(str(a).strip() for a in aliases):
            raise ValueError(f"brand {label!r} needs at least one alias")
        brands.append(Brand(label=label, family=family, pattern=_alternation(str(a) for a in aliases)))

    raw_keywords = data.get("keywords")
    if not isinstance(raw_keywords, dict) or not raw_keywords.get("otp"):
        raise ValueError("SMS patterns need 'keywords.otp'")
    keywords = {}
    for name, phrases in raw_keywords.items():
        if not isinstance(phrases, list):
            raise ValueError(f"keywords.{name} must be a list")
        keywords[name] = tuple(_phrase_pattern(str(p)) for p in phrases if str(p).strip())

    anchors = data.get("trx_anchors")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("SMS patterns need a non-empty 'trx_anchors' list")

    raw_senders = data.get("senders") or {}
    if not isinstance(raw_senders, dict):
        raise ValueError("'senders' must be an object")
    senders = {}
    for address, meta in raw_senders.items():
        if not isinstance(meta, dict):
            raise ValueError(f"sender {address!r} must map to an object")
        label = str(meta.get("label") or "").strip()
        family = str(meta.get("family") or "").strip()
        if not label or family not in DELIVERY_KINDS:
            raise ValueError(f"sender {address!r} needs 'label' and a valid 'family'")
        senders[str(address).strip().upper()] = {"label": label, "family": family}

    return SmsPatterns(
        brands=tuple(brands),
        keywords=keywords,
        trx_anchor=_alternation(anchors, flags=re.IGNORECASE),
        senders=senders,
        source=source,
    )


_pattern_cache: Dict[str, Tuple[int, SmsPatterns]] = {}
_pattern_cache_lock = threading.Lock()


def load_patterns(path: Optional[str] = None) -> SmsPatterns:
    """Load (and cache by mtime) the patterns document.

    mtime_ns is the cache key so editing the file and restarting is enough;
    a long-running worker also picks it up without a restart.
    """
    resolved = os.path.abspath(
        path or os.environ.get("SMS_PATTERNS_FILE") or DEFAULT_PATTERNS_FILE
    )
    try:
        mtime_ns = os.stat(resolved).st_mtime_ns
    except OSError as exc:
        raise RuntimeError(
            f"SMS patterns file is missing or unreadable: {resolved}"
        ) from exc

    with _pattern_cache_lock:
        cached = _pattern_cache.get(resolved)
        if cached and cached[0] == mtime_ns:
            return cached[1]

    try:
        with open(resolved, encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as exc:
        raise RuntimeError(f"SMS patterns file is not valid JSON: {resolved}: {exc}") from exc

    patterns = build_patterns(data, source=resolved)
    with _pattern_cache_lock:
        _pattern_cache[resolved] = (mtime_ns, patterns)
    return patterns


def get_patterns() -> SmsPatterns:
    """The patterns the server should use right now (env override honoured)."""
    return load_patterns()


def _flush_run(best: List[str], run: List[str], saw_word: bool) -> None:
    if saw_word and len(run) >= 4 and len(run) > len(best):
        best[:] = run
    run.clear()


def _number_word_run(lower_text: str) -> str:
    """Digit words joined only by separators: ``ONE_TWO_THREE`` → ``123456``.

    A run must contain at least one *word*; an all-digits run is left to the
    numeric extractors, which can tell an OTP ``123456`` from a date.
    """
    best: List[str] = []
    run: List[str] = []
    saw_word = False
    previous_end = None

    for match in _TOKEN_RE.finditer(lower_text):
        if previous_end is not None and not _SEP_ONLY_RE.fullmatch(
            lower_text[previous_end:match.start()]
        ):
            _flush_run(best, run, saw_word)
            saw_word = False

        token = match.group(0)
        if token.isdigit():
            run.append(token)
        else:
            digit = NUMBER_WORD_TO_DIGIT.get(token)
            if digit is None:
                _flush_run(best, run, saw_word)
                saw_word = False
            else:
                run.append(digit)
                saw_word = True
        previous_end = match.end()

    _flush_run(best, run, saw_word)
    return "".join(best)


def _separated_span(text: str) -> str:
    """A uniformly grouped, separator-joined code: ``1_2_3_4_5_6``.

    The uniform-group rule is what keeps dates (``12-10-2026``) and decimal
    amounts (``1,234.56``) from being read as codes.
    """
    best = ""
    for match in _SEPARATED_SPAN_RE.finditer(text):
        groups = [g for g in _DIGIT_GROUP_SPLIT_RE.split(match.group(0)) if g]
        if len(groups) < 3:
            continue
        lengths = {len(g) for g in groups}
        if len(lengths) != 1 or lengths.pop() > 2:
            continue
        digits = "".join(groups)
        if 4 <= len(digits) <= 8 and len(digits) > len(best):
            best = digits
    return best


def _contiguous_block(text: str, start: int = 0) -> str:
    if start:
        match = _CONTIGUOUS_RE.search(text, start)
        if match:
            return match.group(0)
    matches = _CONTIGUOUS_RE.findall(text)
    return matches[-1] if matches else ""


def _first_keyword(lower_text: str, patterns: Iterable["re.Pattern"]) -> Optional[Tuple[int, int]]:
    earliest = None
    for pattern in patterns:
        match = pattern.search(lower_text)
        if match and (earliest is None or match.start() < earliest[0]):
            earliest = (match.start(), match.end())
    return earliest


def extract_otp_code(message, patterns: Optional[SmsPatterns] = None) -> str:
    """The OTP from a message, in digits, or ``""``.

    Order matters: word runs and explicitly separated digits are unambiguous,
    a contiguous block is not — so when the body names an OTP, the block is
    read from just after that keyword instead of "whichever number is last".
    """
    patterns = patterns or get_patterns()
    text = normalize_text(message)
    lower = text.lower()

    word_run = _number_word_run(lower)
    if word_run:
        return word_run

    separated = _separated_span(text)
    if separated:
        return separated

    anchor = _first_keyword(lower, patterns.keywords.get("otp", ()))
    if anchor:
        block = _contiguous_block(text, start=anchor[1])
        if block:
            return block

    return _contiguous_block(text)


def extract_trx_id(message, patterns: Optional[SmsPatterns] = None) -> str:
    """The transaction ID that follows a ``TrxID`` / ``Txn ID`` anchor.

    The candidate must contain a digit, which is what stops "Transaction ID has
    not been generated" from yielding the word "generated".
    """
    patterns = patterns or get_patterns()
    text = normalize_text(message)

    for anchor in patterns.trx_anchor.finditer(text):
        window = text[anchor.end() : anchor.end() + 24]
        for token in _TRX_TOKEN_RE.finditer(window):
            value = token.group(0)
            if any(char.isdigit() for char in value):
                return value
    return ""


def extract_amount(message) -> str:
    """The first currency-anchored amount, normalised to two decimals."""
    match = _AMOUNT_RE.search(normalize_text(message))
    if not match:
        return ""
    raw = (match.group(1) or match.group(2) or "").replace(",", "")
    try:
        return str(Decimal(raw).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return ""


def _detect_brand(lower_text: str, patterns: SmsPatterns) -> Optional[Brand]:
    if not lower_text:
        return None
    for brand in patterns.brands:
        if brand.pattern.search(lower_text):
            return brand
    return None


def _lookup_sender(raw_sender: str, patterns: SmsPatterns) -> Optional[Dict[str, str]]:
    if not raw_sender:
        return None
    key = raw_sender.strip().upper()
    if key in patterns.senders:
        return patterns.senders[key]

    digits = re.sub(r"\D", "", raw_sender)
    if digits:
        for address, meta in patterns.senders.items():
            if re.sub(r"\D", "", address) == digits:
                return meta
    return None


def parse_sms(
    message,
    sender=None,
    patterns: Optional[SmsPatterns] = None,
) -> ParsedSms:
    """Classify one SMS into a channel and pull out its code and sender.

    ``sender`` is the address the forwarder posted. It is used for the label and
    as a fallback hint only; the body is what decides, because providers change
    sender numbers and the same number can carry several kinds of message.
    """
    patterns = patterns or get_patterns()
    text = normalize_text(message)
    lower = text.lower()
    raw_sender = str(sender or "").strip()

    body_brand = _detect_brand(lower, patterns)
    mapped_sender = _lookup_sender(raw_sender, patterns)
    sender_brand = _detect_brand(raw_sender.lower(), patterns)

    if body_brand:
        family, sender_label = body_brand.family, body_brand.label
    elif mapped_sender:
        family, sender_label = mapped_sender["family"], mapped_sender["label"]
    elif sender_brand:
        family, sender_label = sender_brand.family, sender_brand.label
    else:
        family, sender_label = "", raw_sender

    otp = extract_otp_code(text, patterns)
    trx = extract_trx_id(text, patterns)
    amount = extract_amount(text)

    otp_keyword = _first_keyword(lower, patterns.keywords.get("otp", ()))
    signin_keyword = _first_keyword(lower, patterns.keywords.get("signin", ()))

    kind = KIND_UNKNOWN
    reason = "no OTP or transaction identifier found"

    if otp and otp_keyword:
        if family == KIND_PAYMENT:
            kind, reason = KIND_PAYMENT, "payment OTP (brand or sender says payment)"
        elif family == KIND_SIGNIN:
            kind, reason = KIND_SIGNIN, "sign-in OTP (brand or sender says sign-in)"
        elif signin_keyword:
            kind, reason = KIND_SIGNIN, "sign-in OTP (sign-in vocabulary in body)"
        else:
            reason = "OTP keyword and a code, but no recognised brand or sender"
    elif trx:
        kind, reason = KIND_TRANSACTION, "transaction ID"
    elif otp:
        reason = "code found but the body never calls it an OTP"

    # Only publish the fields that belong to the chosen channel: an "unknown"
    # message must not hand a consumer a neighbouring number as if it were an
    # OTP, and an amount must never masquerade as one.
    if kind == KIND_TRANSACTION:
        otp = ""
    elif kind in (KIND_SIGNIN, KIND_PAYMENT):
        trx, amount = "", ""
    else:
        otp = trx = amount = ""

    return ParsedSms(
        kind=kind,
        code=otp or trx,
        otp=otp,
        trx=trx,
        amount=amount,
        sender=sender_label,
        sender_number=raw_sender,
        reason=reason,
    )
