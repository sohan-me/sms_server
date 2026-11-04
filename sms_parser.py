"""Turn one inbound SMS into a sign-in OTP decision and a sender label.

The server used to treat every inbound message as a single OTP stream: it
scanned the body for a digit run and pushed it to every subscribed client.
That hard-codes one IVAC format and cannot tell an OTP from a receipt, so the
body is now classified and parsed instead.

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
from typing import Dict, Iterable, List, Optional, Tuple

from otp_core import NUMBER_WORD_TO_DIGIT

logger = logging.getLogger(__name__)

KIND_SIGNIN = "signin"
KIND_UNKNOWN = "unknown"

# The kinds that have a public endpoint and a WebSocket channel.
DELIVERY_KINDS = (KIND_SIGNIN,)

DEFAULT_PATTERNS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sms_patterns.json"
)

# Bengali digits appear in some operator templates; normalise them before any
# pattern runs so one rule covers both scripts.
_BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# The separators providers actually alternate between.
_SEPARATOR_CHARS = r"\s\-_.·•/,:\+\*~|#=\\^%&@;!"
_SEP_ONLY_RE = re.compile(r"[\s\-_.·•/,:\+\*~|#=\\^%&@;!]*")
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
_ALPHANUM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{4,8}(?![A-Za-z0-9])")
_ALPHA_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{4,8}(?![A-Za-z0-9])")
# A code split into groups: "A-1-g-0-G-i-7" (one character per group) and
# "A1-g0-Gi7" (several). Groups are separated by the same characters, so the
# grouping alone cannot be used to tell a code from a sentence — see
# _is_code_group for what may join a run.
_ALNUM_GROUP_RE = re.compile(r"[A-Za-z0-9]+")
# The clause a security warning runs to (end of sentence/line).
_WARNING_END_RE = re.compile(r"[.!?\n]")
# A currency amount or a balance. A message carrying one of these and no OTP
# keyword is a receipt, and the long digit runs in it are not a code.
_MONEY_RE = re.compile(
    r"(?<![a-z])(?:tk|taka|bdt|৳)(?![a-z])|\bbalance\b", re.IGNORECASE
)
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
    "code", "pin", "pass", "word", "three", "four", "five", "prize", "today",
    "is", "in", "to", "for", "by", "on", "at", "an", "the", "be", "do", "no", "if",
    "or", "as", "so", "up", "and", "of", "it", "my", "me", "we", "he", "us", "are",
    "enter", "type", "input", "here"
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
    r"[\s:=,\-\./\+\*]*"
    r"(?:is|are|to|for|when\s+prompted)?"
    r"[\s:=,\-\./\+\*]*"
    r"([A-Za-z0-9]{4,8})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedSms:
    """One message's routing decision.

    ``otp`` and ``code`` are only set for a sign-in OTP, so a consumer can
    never mistake some other number for a code.
    """

    kind: str = KIND_UNKNOWN
    code: str = ""
    otp: str = ""
    sender: str = ""
    sender_number: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Brand:
    label: str
    pattern: "re.Pattern"


@dataclass(frozen=True)
class SmsPatterns:
    brands: Tuple[Brand, ...]
    keywords: Dict[str, Tuple["re.Pattern", ...]]
    senders: Dict[str, str]
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
        aliases = entry.get("aliases")
        if not label:
            raise ValueError("every brand needs a 'label'")
        if not isinstance(aliases, list) or not any(str(a).strip() for a in aliases):
            raise ValueError(f"brand {label!r} needs at least one alias")
        brands.append(Brand(label=label, pattern=_alternation(str(a) for a in aliases)))

    raw_keywords = data.get("keywords")
    if not isinstance(raw_keywords, dict) or not raw_keywords.get("otp"):
        raise ValueError("SMS patterns need 'keywords.otp'")
    keywords = {}
    for name, phrases in raw_keywords.items():
        if not isinstance(phrases, list):
            raise ValueError(f"keywords.{name} must be a list")
        keywords[name] = tuple(_phrase_pattern(str(p)) for p in phrases if str(p).strip())

    raw_senders = data.get("senders") or {}
    if not isinstance(raw_senders, dict):
        raise ValueError("'senders' must be an object")
    senders = {}
    for address, raw_label in raw_senders.items():
        label = str(raw_label or "").strip()
        if not label:
            raise ValueError(f"sender {address!r} needs a 'label'")
        senders[str(address).strip().upper()] = label

    return SmsPatterns(
        brands=tuple(brands),
        keywords=keywords,
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
    if saw_word and 4 <= len(run) <= 8 and len(run) > len(best):
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


def _group_kind(token: str) -> str:
    """How one separator-delimited group can take part in a code run."""
    lower = token.lower()
    if lower in COMMON_STOP_WORDS:
        return "word"
    has_letter = any(c.isalpha() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if has_letter and has_digit:
        return "mixed"
    if has_digit:
        return "digits"
    if len(token) == 1:
        return "char"
    if len(token) <= 4:
        return "letters"
    return "word"


def _separated_alnum_run(text: str) -> str:
    """A mixed letter/digit code split into groups.

    IVAC separates the characters of an alphanumeric code the way it used to
    separate digits — ``A-1-g-0-G-i-7`` — and also groups them, as in
    ``A1-g0-Gi7``, in the same 4–8 character range. The digit-only span reader
    saw ``1 2 3 4 F g L`` as the two-group span ``1234`` and returned a
    truncated code, and an all-character body such as ``A 1 g 0 G i 7``
    yielded nothing at all.
    """
    best = ""
    current: List[str] = []
    seen_mixed = False

    def flush() -> None:
        nonlocal best, seen_mixed
        joined = "".join(current)
        if (
            len(current) >= 2
            and 4 <= len(joined) <= 8
            and any(c.isalpha() for c in joined)
            and any(c.isdigit() for c in joined)
            and len(joined) > len(best)
        ):
            best = joined
        current.clear()
        seen_mixed = False

    previous_end = None
    for match in _ALNUM_GROUP_RE.finditer(text):
        sep_text = text[previous_end:match.start()] if previous_end is not None else ""
        if previous_end is not None and not _SEP_ONLY_RE.fullmatch(sep_text):
            flush()
        token = match.group(0)
        kind = _group_kind(token)
        has_punct_sep = any(c in r"\-_./+*~|#=:" for c in sep_text)
        if kind == "word":
            flush()
        elif kind == "letters":
            if seen_mixed or has_punct_sep:
                current.append(token)
                seen_mixed = True
        elif kind == "digits":
            if current and has_punct_sep and any(c.isalpha() for c in "".join(current)):
                seen_mixed = True
            current.append(token)
        else:
            current.append(token)
            seen_mixed = seen_mixed or kind == "mixed"
        previous_end = match.end()

    flush()
    return best


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
        if saw_compound and 4 <= len(res) <= 8 and len(res) > len(best_digits):
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
            if candidate.lower().startswith(prefix):
                rest = candidate[len(prefix):]
                if 4 <= len(rest) <= 8 and rest.lower() not in COMMON_STOP_WORDS:
                    return rest
        if candidate.lower() not in COMMON_STOP_WORDS:
            return candidate
    return ""


def _extract_alphanumeric_otp(text: str) -> str:
    """Standalone alphanumeric token: letters + digits, 4-8 chars, any case."""
    for m in re.finditer(r"\b(?:otp|pin|code)([A-Za-z0-9]{4,8})\b", text, re.IGNORECASE):
        candidate = m.group(1)
        if candidate.lower() not in COMMON_STOP_WORDS:
            return candidate

    for m in _ALPHANUM_RE.finditer(text):
        tok = m.group(0)
        if any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
            for prefix in ("otp", "pin", "code"):
                if tok.lower().startswith(prefix):
                    rest = tok[len(prefix):]
                    if 4 <= len(rest) <= 8 and rest.lower() not in COMMON_STOP_WORDS:
                        return rest
            if tok.lower() not in COMMON_STOP_WORDS:
                return tok
    return ""


def _extract_alphabet_only_otp(text: str) -> str:
    """Alphabet-only token: letters only, 4-10 chars, any case (e.g. AbCdEfGh)."""
    stripped = text.strip()
    if re.fullmatch(r"[A-Za-z]{4,8}", stripped) and stripped.lower() not in COMMON_STOP_WORDS:
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
    """The OTP from a message following all 8 formats in order.

    Every format accepts 4–8 characters; anything longer is not a code this
    server forwards.

    1. Worded digits (Zero–Nine) — any separator (- _ space / . , :)
    2. Separated groups — A 1 g 0 G i 7, A1-g0-Gi7, letters + digits, 4–8 chars
    3. Labeled — OTP / code / pin / password / passcode / verification code + value
    4. Spaced/hyphen digits — 4–8 digits with spaces/hyphens
    5. Alphanumeric — letters + digits, 4–8 chars, any case
    6. Numeric — plain 4–8 digits
    7. Compound worded — Ten–Ninety style chunks
    8. Alphabet-only — letters only, 4–8 chars, any case
    """
    patterns = patterns or get_patterns()
    text = normalize_text(message)
    lower = text.lower()

    # 1. Worded digits (Zero–Nine) — any separator (- _ space / . , :)
    word_run = _number_word_run(lower)
    if word_run:
        return word_run

    # 2. Separated groups — A 1 g 0 G i 7, A1-g0-Gi7, letters + digits, 4–8 chars
    #    Before the labeled reader on purpose: "code: AB12-CD3" would otherwise
    #    stop at the first group and report "AB12".
    spaced_alnum = _separated_alnum_run(text)
    if spaced_alnum:
        return spaced_alnum

    # 3. Labeled — OTP / code / pin / password / passcode / verification code + value
    labeled = _extract_labeled_otp(text)
    if labeled:
        return labeled

    # 4. Spaced/hyphen digits — 4–8 digits with spaces/hyphens
    separated = _separated_span(text)
    if separated:
        return separated

    # 5. Alphanumeric — letters + digits, 4–8 chars, any case
    alnum = _extract_alphanumeric_otp(text)
    if alnum:
        return alnum

    # 6. Numeric — plain 4–8 digits
    anchor = _otp_anchor(lower, patterns)
    if anchor:
        block = _contiguous_block(text, start=anchor[1])
        if block:
            return block
    numeric = _contiguous_block(text)
    if numeric:
        return numeric

    # 7. Compound worded — Ten–Ninety style chunks
    compound = _compound_word_run(lower)
    if compound:
        return compound

    # 8. Alphabet-only — letters only, 4–8 chars, any case
    alpha = _extract_alphabet_only_otp(text)
    if alpha:
        return alpha

    return ""


def _detect_brand(
    text: str,
    patterns: SmsPatterns,
    anchor: Optional[Tuple[int, int]] = None,
) -> Optional[Brand]:
    """The brand a message is about, or ``None``.

    ``anchor`` is the OTP keyword's span. With it, the brand *nearest the
    keyword* wins instead of the first one listed: a message can name a
    gateway and a merchant, and only proximity tells them apart.
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


def _lookup_sender(raw_sender: str, patterns: SmsPatterns) -> Optional[str]:
    """The label a sender address belongs to (IVAC id, IVACBD, ...)."""
    if not raw_sender:
        return None
    key = raw_sender.strip().upper()
    if key in patterns.senders:
        return patterns.senders[key]

    digits = _sender_digits(raw_sender)
    if digits:
        for address, label in patterns.senders.items():
            if _sender_digits(address) == digits:
                return label
    return None


def parse_sms(
    message,
    sender=None,
    patterns: Optional[SmsPatterns] = None,
) -> ParsedSms:
    """Classify one SMS as a sign-in OTP and pull out its code and sender.

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
        family, sender_label = KIND_SIGNIN, body_brand.label
    elif mapped_sender:
        family, sender_label = KIND_SIGNIN, mapped_sender
    elif sender_brand:
        family, sender_label = KIND_SIGNIN, sender_brand.label
    else:
        family, sender_label = "", raw_sender

    otp = extract_otp_code(text, patterns)
    signin_keyword = _first_keyword(lower, patterns.keywords.get("signin", ()))

    # A receipt is not an OTP: "Payment of Tk 3,000.00 ... Balance Tk 11,620.91"
    # holds enough digit runs to be read as a code, and none of them is one. Only
    # an OTP keyword makes a money message an OTP.
    if otp and not otp_keyword and _MONEY_RE.search(lower):
        otp = ""

    kind = KIND_UNKNOWN
    reason = "no OTP found"

    # An OTP sequence / code is what makes a message a sign-in OTP.
    has_otp_sequence = bool(
        otp
        and (
            otp_keyword
            or _number_word_run(lower)
            or _extract_labeled_otp(text)
            or _separated_alnum_run(text)
            or _separated_span(text)
            or _extract_alphanumeric_otp(text)
            or _compound_word_run(lower)
            or _extract_alphabet_only_otp(text)
            or re.fullmatch(r"[A-Za-z0-9]{4,8}", text.strip())
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
            reason = "sign-in OTP (no provider brand in the body)"

        # An unmapped sender echoes its own address; label those as IVAC.
        if not sender_label or sender_label == raw_sender:
            sender_label = "IVAC"
    elif otp:
        reason = "code found but the body never calls it an OTP"

    # An "unknown" message must not hand a consumer a neighbouring number as
    # if it were an OTP.
    if kind != KIND_SIGNIN:
        otp = ""

    return ParsedSms(
        kind=kind,
        code=otp,
        otp=otp,
        sender=sender_label,
        sender_number=raw_sender,
        reason=reason,
    )
