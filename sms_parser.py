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

# The separators providers actually alternate between.
_SEPARATOR_CHARS = r"\s\-_.·•/,:"
_SEP_ONLY_RE = re.compile(r"[\s\-_.·•/,:]*")
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
_ALPHANUM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{4,10}(?![A-Za-z0-9])")
_ALPHA_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{4,10}(?![A-Za-z0-9])")
_TRX_TOKEN_RE = re.compile(r"[A-Za-z0-9]{6,20}")
_AMOUNT_RE = re.compile(
    # "Tk.3,000.00" and "Tk 3,000.00" are the same amount: bKash writes the
    # dot, and a literal "\s*" after the currency would miss every one of them.
    r"(?:(?:bdt|tk|taka|৳)\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:bdt|tk|taka|৳))",
    re.IGNORECASE,
)
# The clause a security warning runs to (end of sentence/line).
_WARNING_END_RE = re.compile(r"[.!?\n]")
# How close a code must sit to a warning-covered OTP keyword before it counts as
# that keyword's code ("Do not share your OTP 123456 with anyone").
_OTP_WARNING_WINDOW = 24

COMMON_STOP_WORDS = {
    "your", "this", "that", "with", "from", "have", "been", "sent", "will", "expires",
    "please", "welcome", "security", "message", "service", "services", "account",
    "prompted", "sequence", "following", "valid", "minutes", "verification",
    "password", "passcode", "number", "payment", "purchase", "amount",
    "balance", "received", "successful", "share", "anyone", "helpline",
    "banking", "mobile", "gateway", "offer", "cashback", "transaction",
    "secret", "detail", "details", "contact", "update", "online",
    "nagad", "bkash", "rocket", "dbbl", "upay", "dutch", "bangla",
    "code", "pin", "pass", "word", "three", "four", "five", "prize", "today"
}

COMPOUND_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
COMPOUND_TEENS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19",
}
COMPOUND_UNITS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

LABELED_DIRECT_RE = re.compile(
    r"\b(?:otp|verification\s+code|verify\s+code|security\s+code|login\s+code|"
    r"confirmation\s+code|passcode|password|pin|your\s+code|code|following\s+sequence|"
    r"sequence\s+when\s+prompted|type\s+the\s+following\s+sequence|security\s+sequence)\b"
    r"[\s:=,\-]*(?:is|are|to|for|when\s+prompted)?[\s:=,\-]*"
    r"([A-Za-z0-9]{4,10})\b",
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
    """A uniformly grouped, separator-joined code: ``1_2_3_4_5_6``, ``1 2 3 4 5 6``.

    The uniform-group rule is what keeps dates (``12-10-2026``) and decimal
    amounts (``1,234.56``) from being read as codes.
    """
    best = ""
    for match in _SEPARATED_SPAN_RE.finditer(text):
        groups = [g for g in _DIGIT_GROUP_SPLIT_RE.split(match.group(0)) if g]
        if len(groups) < 2:
            continue
        # Avoid dates like 31-12-2026 or 2026-12-31
        if len(groups) == 3 and (len(groups[0]) == 4 or len(groups[2]) == 4):
            continue
        lengths = {len(g) for g in groups}
        if len(lengths) > 2 or (len(lengths) == 2 and max(lengths) - min(lengths) > 1):
            continue
        digits = "".join(groups)
        if 4 <= len(digits) <= 8 and len(digits) > len(best):
            best = digits
    return best


def _compound_word_run(lower_text: str) -> str:
    """Compound worded chunks: Ten Eleven Eighty Six -> 101186."""
    last_end = None
    best_digits = ""
    current_tokens = []

    def flush(curr):
        nonlocal best_digits
        if not curr:
            return
        out = []
        i = 0
        saw_compound = False
        while i < len(curr):
            w = curr[i]
            if w in COMPOUND_TENS:
                saw_compound = True
                if (
                    i + 1 < len(curr)
                    and curr[i + 1] in COMPOUND_UNITS
                    and COMPOUND_UNITS[curr[i + 1]] > 0
                ):
                    out.append(str(COMPOUND_TENS[w] + COMPOUND_UNITS[curr[i + 1]]))
                    i += 2
                else:
                    out.append(str(COMPOUND_TENS[w]))
                    i += 1
            elif w in COMPOUND_TEENS:
                saw_compound = True
                out.append(COMPOUND_TEENS[w])
                i += 1
            elif w in COMPOUND_UNITS:
                out.append(str(COMPOUND_UNITS[w]))
                i += 1
            elif w.isdigit():
                out.append(w)
                i += 1
            else:
                i += 1
        res = "".join(out)
        if saw_compound and 4 <= len(res) <= 10 and len(res) > len(best_digits):
            best_digits = res

    for match in _TOKEN_RE.finditer(lower_text):
        if last_end is not None and not _SEP_ONLY_RE.fullmatch(
            lower_text[last_end:match.start()]
        ):
            flush(current_tokens)
            current_tokens = []
        tok = match.group(0)
        if (
            tok in COMPOUND_TENS
            or tok in COMPOUND_TEENS
            or tok in COMPOUND_UNITS
            or tok.isdigit()
        ):
            current_tokens.append(tok)
        else:
            flush(current_tokens)
            current_tokens = []
        last_end = match.end()
    flush(current_tokens)
    return best_digits


def _extract_labeled_otp(text: str) -> str:
    """Extract directly labeled OTP: OTP is Ab76gTf, Code: 123456, etc."""
    for m in LABELED_DIRECT_RE.finditer(text):
        candidate = m.group(1)
        for prefix in ("otp", "pin", "code"):
            if candidate.lower().startswith(prefix) and candidate[len(prefix):].isdigit():
                return candidate[len(prefix):]
        if candidate.lower() not in COMMON_STOP_WORDS:
            return candidate
    return ""


def _extract_alphanumeric_otp(text: str) -> str:
    """Standalone alphanumeric token: letters + digits, 4-10 chars, any case."""
    for m in _ALPHANUM_RE.finditer(text):
        tok = m.group(0)
        if any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
            for prefix in ("otp", "pin", "code"):
                if tok.lower().startswith(prefix) and tok[len(prefix):].isdigit():
                    return tok[len(prefix):]
            if tok.lower() not in COMMON_STOP_WORDS:
                return tok
    return ""


def _extract_alphabet_only_otp(text: str) -> str:
    """Alphabet-only token: letters only, 4-10 chars, any case (e.g. AbCdEfGh)."""
    stripped = text.strip()
    if re.fullmatch(r"[A-Za-z]{4,10}", stripped) and stripped.lower() not in COMMON_STOP_WORDS:
        return stripped
    for m in _ALPHA_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() not in COMMON_STOP_WORDS:
            if any(c.isupper() for c in tok) and any(c.islower() for c in tok):
                return tok
    return ""


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


def _warning_spans(lower_text: str, patterns: SmsPatterns) -> List[Tuple[int, int]]:
    """The security-warning clauses ("do not share your OTP ...") in a body.

    Every provider prefixes an OTP with one, and the wallets also put the same
    line on a completed-transaction SMS. Reading the keyword inside it as an
    OTP announcement is what made "Never share your OTP ... TrxID 9HAB ...
    Balance Tk 1500" a payment OTP of 1500 instead of a transaction.
    """
    spans = []
    for pattern in patterns.keywords.get("warning", ()):
        for match in pattern.finditer(lower_text):
            end_match = _WARNING_END_RE.search(lower_text, match.end())
            end = end_match.start() + 1 if end_match else len(lower_text)
            spans.append((match.start(), end))
    return spans


def _otp_anchor(lower_text: str, patterns: SmsPatterns) -> Optional[Tuple[int, int]]:
    """The OTP keyword a message announces its code with, not a warning.

    A warning is still the only keyword when a template puts the code in the
    warning itself, so that case survives — but only when a code sits within a
    few characters of it.
    """
    spans = _warning_spans(lower_text, patterns)

    def in_warning(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    primary = None
    fallback = None
    for pattern in patterns.keywords.get("otp", ()):
        for match in pattern.finditer(lower_text):
            span = (match.start(), match.end())
            if in_warning(match.start()):
                if fallback is None or span[0] < fallback[0]:
                    fallback = span
            elif primary is None or span[0] < primary[0]:
                primary = span

    if primary:
        return primary
    if fallback and _CONTIGUOUS_RE.search(
        lower_text, fallback[1], fallback[1] + _OTP_WARNING_WINDOW
    ):
        return fallback
    return None


def extract_otp_code(message, patterns: Optional[SmsPatterns] = None) -> str:
    """The OTP from a message following all 7 formats in order:
    1. Worded digits (Zero–Nine) — any separator (- _ space / . , :)
    2. Labeled — OTP / code / pin / password / passcode / verification code + value
    3. Spaced/hyphen digits — 4–8 digits with spaces/hyphens
    4. Alphanumeric — letters + digits, 4–10 chars, any case
    5. Numeric — plain 4–8 digits
    6. Compound worded — Ten–Ninety style chunks
    7. Alphabet-only — letters only, 4–10 chars, any case
    """
    patterns = patterns or get_patterns()
    text = normalize_text(message)
    lower = text.lower()

    # 1. Worded digits (Zero–Nine) — any separator (- _ space / . , :)
    word_run = _number_word_run(lower)
    if word_run:
        return word_run

    # 2. Labeled — OTP / code / pin / password / passcode / verification code + value
    labeled = _extract_labeled_otp(text)
    if labeled:
        return labeled

    # 3. Spaced/hyphen digits — 4–8 digits with spaces/hyphens
    separated = _separated_span(text)
    if separated:
        return separated

    # 4. Alphanumeric — letters + digits, 4–10 chars, any case
    alnum = _extract_alphanumeric_otp(text)
    if alnum:
        return alnum

    # 5. Numeric — plain 4–8 digits
    anchor = _otp_anchor(lower, patterns)
    if anchor:
        block = _contiguous_block(text, start=anchor[1])
        if block:
            return block
    numeric = _contiguous_block(text)
    if numeric:
        return numeric

    # 6. Compound worded — Ten–Ninety style chunks
    compound = _compound_word_run(lower)
    if compound:
        return compound

    # 7. Alphabet-only — letters only, 4–10 chars, any case
    alpha = _extract_alphabet_only_otp(text)
    if alpha:
        return alpha

    return ""


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


def _detect_brand(
    text: str,
    patterns: SmsPatterns,
    anchor: Optional[Tuple[int, int]] = None,
) -> Optional[Brand]:
    """The brand a message is about, or ``None``.

    ``anchor`` is the OTP keyword's span. With it, the brand *nearest the
    keyword* wins instead of the first one listed: a payment OTP names its
    merchant too ("Your bKash OTP for PAYMENT of Tk 3,000.00 to IVAC ..."),
    and only proximity tells the wallet apart from the payee.
    """
    if not text:
        return None

    best: Optional[Tuple[Tuple[int, int], Brand]] = None
    for index, brand in enumerate(patterns.brands):
        match = brand.pattern.search(text)
        if match is None:
            continue
        if anchor is None:
            # Nothing to measure against, so the listed order decides.
            return brand
        if match.end() <= anchor[0]:
            distance = anchor[0] - match.end()
        elif match.start() >= anchor[1]:
            distance = match.start() - anchor[1]
        else:
            distance = 0
        key = (distance, index)
        if best is None or key < best[0]:
            best = (key, brand)
    return best[1] if best else None


def _sender_digits(value) -> str:
    """A sender address's digits, with country code and leading zero removed.

    One forwarder reports a shortcode as ``16216``, another as ``+880 16216``;
    the senders map has to treat those as the same address.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("880") and len(digits) > 6:
        digits = digits[3:]
    return digits.lstrip("0")


def _lookup_sender(raw_sender: str, patterns: SmsPatterns) -> Optional[Dict[str, str]]:
    """The brand a sender address belongs to (wallet shortcode, IVAC id, ...)."""
    if not raw_sender:
        return None
    key = raw_sender.strip().upper()
    if key in patterns.senders:
        return patterns.senders[key]

    digits = _sender_digits(raw_sender)
    if digits:
        for address, meta in patterns.senders.items():
            if _sender_digits(address) == digits:
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

    otp_keyword = _otp_anchor(lower, patterns)

    body_brand = _detect_brand(lower, patterns, anchor=otp_keyword)
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

    signin_keyword = _first_keyword(lower, patterns.keywords.get("signin", ()))
    payment_keyword = _first_keyword(lower, patterns.keywords.get("payment", ()))

    # MFS (Mobile Financial Services) detection:
    # A message is from an MFS payment provider if body or sender matches a payment brand/shortcode.
    sender_is_mfs = (mapped_sender and mapped_sender["family"] == KIND_PAYMENT) or (
        sender_brand and sender_brand.family == KIND_PAYMENT
    )
    body_is_mfs = bool(body_brand and body_brand.family == KIND_PAYMENT)
    is_mfs = sender_is_mfs or body_is_mfs

    # A wallet message can name only its payee: "Your OTP for PAYMENT of
    # Tk.3,000.00 to IVAC (DGePay) is 319805" is bKash's payment OTP, and
    # the only brand in the body belongs to the merchant. The forwarder's
    # sender number or brand is what says which wallet actually sent it.
    if family == KIND_SIGNIN and sender_is_mfs and payment_keyword:
        family = KIND_PAYMENT
        sender_label = (mapped_sender and mapped_sender["label"]) or (
            sender_brand and sender_brand.label
        ) or "bKash"
        is_mfs = True

    kind = KIND_UNKNOWN
    reason = "no OTP or transaction identifier found"

    # 1. First, check if it is an MFS transaction or payment OTP
    if is_mfs or trx:
        if otp and otp_keyword and family == KIND_PAYMENT:
            kind, reason = KIND_PAYMENT, "payment OTP (brand or sender says payment)"
        elif trx:
            kind, reason = KIND_TRANSACTION, "transaction ID"
            if not sender_label or sender_label == raw_sender:
                if mapped_sender:
                    sender_label = mapped_sender["label"]
                elif sender_brand:
                    sender_label = sender_brand.label
        elif otp and otp_keyword:
            kind, reason = KIND_PAYMENT, "payment OTP (MFS payment OTP)"
        elif otp:
            reason = "code found but the body never calls it an OTP"
    else:
        # 2. Not from MFS: if an OTP sequence / code is found, route to IVAC sign-in OTP
        has_otp_sequence = bool(
            otp
            and (
                otp_keyword
                or _number_word_run(lower)
                or _extract_labeled_otp(text)
                or _separated_span(text)
                or _extract_alphanumeric_otp(text)
                or _compound_word_run(lower)
                or _extract_alphabet_only_otp(text)
                or re.fullmatch(r"[A-Za-z0-9]{4,10}", text.strip())
                or family == KIND_SIGNIN
                or signin_keyword
            )
        )

        if has_otp_sequence and otp:
            kind = KIND_SIGNIN
            if family == KIND_SIGNIN:
                reason = "sign-in OTP (brand or sender says sign-in)"
            elif signin_keyword:
                reason = "sign-in OTP (sign-in vocabulary in body)"
            else:
                reason = "sign-in OTP (IVAC default for non-MFS OTP)"

            # Ensure sender is labeled as IVAC if unknown or unmapped
            if not sender_label or sender_label == raw_sender or sender_label not in ("IVAC", "IVACBD"):
                sender_label = "IVAC"
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
