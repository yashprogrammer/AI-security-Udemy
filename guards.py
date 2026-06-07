"""
guards.py — the validation engine behind the Streamlit demo.

Mirrors the patterns from `Notebook/Guardrails ai/guardrails_nimbuspay_v3.ipynb`:
- Real Guardrails Hub validators that are light: DetectPII, CompetitorCheck.
- Real custom validators: refund_disclaimer (function form) + MaxRefundClaim (class form).
- LLM-as-judge (Groq) for toxicity & topic — no ML weights, keeps us under 1 GB on
  Streamlit Community Cloud.

Hub validators install via the `guardrails hub install` CLI, which Streamlit Cloud does
NOT run automatically. `setup_guardrails()` performs that install once per cold start and
degrades gracefully (regex / substring fallbacks) if it fails.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import tempfile

import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Shared constants (match the notebook)
# ──────────────────────────────────────────────────────────────────────────────
BOT_MODEL = "groq/llama-3.3-70b-versatile"

SYSTEM_PROMPT = (
    "You are NimbusPay's customer-support assistant. "
    "NimbusPay is a digital payments app: cards, wallets, refunds, account help. "
    "Be concise, friendly, and accurate."
)

COMPETITORS = ["Razorpay", "PayU", "Stripe", "PayPal"]
PII_ENTITIES = ["CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER"]


# ──────────────────────────────────────────────────────────────────────────────
# Key handling — st.secrets on Cloud, os.environ locally
# ──────────────────────────────────────────────────────────────────────────────
def get_key(name: str) -> str | None:
    """Read a key from Streamlit secrets first, then the environment."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name)


def apply_keys(groq_override: str | None = None,
               guardrails_override: str | None = None) -> dict:
    """Resolve keys (UI override → secrets → env) and push them into the environment
    so litellm / guardrails pick them up.

    Pass values typed into the sidebar as overrides; they win over secrets/env.
    Returns a dict of which keys ended up present.
    """
    groq = groq_override or get_key("GROQ_API_KEY")
    grd = guardrails_override or get_key("GUARDRAILS_TOKEN")
    if groq:
        os.environ["GROQ_API_KEY"] = groq
    else:
        os.environ.pop("GROQ_API_KEY", None)
    if grd:
        os.environ["GUARDRAILS_TOKEN"] = grd
    else:
        os.environ.pop("GUARDRAILS_TOKEN", None)
    return {"groq": bool(groq), "guardrails": bool(grd)}


# ──────────────────────────────────────────────────────────────────────────────
# One-time Guardrails setup: install the two light Hub validators (cached)
# ──────────────────────────────────────────────────────────────────────────────
# Why not `guardrails hub install`?  That CLI installs the validator into the
# interpreter's site-packages AND appends an import line to
# `guardrails/hub/__init__.py`. On Streamlit Community Cloud the venv is *read-only*
# at runtime, so both writes fail with "Permission denied (.../venv/.lock)".
#
# Instead we pip-install the validator's private package straight into a writable
# /tmp directory (`--target`) and import the module directly — `DetectPII` lives in
# `guardrails_grhub_detect_pii`, which is exactly the import the hub CLI would append.
# Hub validator packages live on Guardrails' private index and need the user's token.
HUB_VALIDATORS = {
    # status-key: (pip package name, importable module, exported class)
    "pii": ("guardrails-grhub-detect-pii", "guardrails_grhub_detect_pii", "DetectPII"),
    "competitor": (
        "guardrails-grhub-competitor-check",
        "guardrails_grhub_competitor_check",
        "CompetitorCheck",
    ),
}

_HUB_TARGET = os.path.join(tempfile.gettempdir(), "gr_hub_validators")
_NLTK_DATA = os.path.join(tempfile.gettempdir(), "nltk_data")

_PRESIDIO_PATCHED = False


def _pin_presidio_to_small_spacy() -> None:
    """Force presidio-analyzer to use the small spaCy model (en_core_web_sm).

    Why this exists: `DetectPII()` constructs `AnalyzerEngine()` with no args, and
    presidio's default config asks for `en_core_web_lg` (~400 MB). On Streamlit
    Community Cloud that (a) blows the 1 GB RAM cap and (b) can't be auto-downloaded
    anyway because site-packages is read-only. The wheel for `en_core_web_sm` is
    already in `requirements.txt`, so we pin presidio to it.

    Idempotent; safe to call multiple times.
    """
    global _PRESIDIO_PATCHED
    if _PRESIDIO_PATCHED:
        return
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError:
        return

    _orig_init = AnalyzerEngine.__init__

    def _patched_init(self, *args, **kwargs):
        if "nlp_engine" not in kwargs and (len(args) < 1 or args[0] is None):
            kwargs["nlp_engine"] = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [
                        {"lang_code": "en", "model_name": "en_core_web_sm"},
                    ],
                }
            ).create_engine()
        return _orig_init(self, *args, **kwargs)

    AnalyzerEngine.__init__ = _patched_init
    _PRESIDIO_PATCHED = True


def _ensure_nltk_data() -> None:
    """`competitor_check` calls `nltk.tokenize.sent_tokenize`, which needs the
    `punkt` corpus. The hub CLI normally downloads it via the package's
    `post_install` script; we do the same into a writable /tmp dir."""
    os.environ.setdefault("NLTK_DATA", _NLTK_DATA)
    try:
        import nltk  # noqa: F401  (only present once the validator deps install)
    except ImportError:
        return
    os.makedirs(_NLTK_DATA, exist_ok=True)
    if _NLTK_DATA not in nltk.data.path:
        nltk.data.path.insert(0, _NLTK_DATA)
    # `punkt_tab` is the NLTK ≥3.9 layout; older `punkt` still ships for back-compat.
    for corpus in ("punkt_tab", "punkt"):
        try:
            nltk.download(corpus, download_dir=_NLTK_DATA, quiet=True)
        except Exception:  # noqa: BLE001
            pass


def _hub_module(status_key: str):
    """Import a Hub validator's class — from site-packages (local) or the /tmp
    target we pip-installed into (Cloud). Returns the class or raises ImportError."""
    _pkg, module, cls = HUB_VALIDATORS[status_key]
    # Prefer the canonical guardrails.hub path (present after a local `hub install`).
    try:
        hub = importlib.import_module("guardrails.hub")
        if hasattr(hub, cls):
            return getattr(hub, cls)
    except Exception:  # noqa: BLE001
        pass
    if _HUB_TARGET not in sys.path:
        sys.path.insert(0, _HUB_TARGET)
    return getattr(importlib.import_module(module), cls)


@st.cache_resource(show_spinner="Installing Guardrails Hub validators…")
def setup_guardrails(token: str | None) -> dict:
    """Install the two light Hub validators into a writable /tmp target and confirm
    they import.

    `token` is an explicit parameter (not read from env) so Streamlit's cache
    re-runs the install whenever the user enters/changes the token in the sidebar.

    Returns {"pii": bool, "competitor": bool, "error": str | None}. On any failure
    the caller falls back to regex / substring matching, so the app never hard-fails.
    """
    status = {"pii": False, "competitor": False, "error": None}

    if not token:
        status["error"] = "No Guardrails token — using regex/substring fallbacks."
        return status

    # Apply the presidio small-spaCy pin BEFORE any DetectPII import constructs
    # an AnalyzerEngine — otherwise it tries to download en_core_web_lg (400 MB).
    _pin_presidio_to_small_spacy()

    os.makedirs(_HUB_TARGET, exist_ok=True)
    if _HUB_TARGET not in sys.path:
        sys.path.insert(0, _HUB_TARGET)

    index = f"https://__token__:{token}@pypi.guardrailsai.com/simple"
    errors = []

    for key, (pkg, module, _cls) in HUB_VALIDATORS.items():
        # Already importable (e.g. a prior local `guardrails hub install`)?
        try:
            _hub_module(key)
            status[key] = True
            continue
        except Exception:  # noqa: BLE001
            pass

        installed = False
        last_err = ""
        # Mirror the hub CLI: try the `[validators]` extra first, then the bare name.
        # NOTE: we *do not* pass `--no-deps`: the validator's runtime deps (e.g. nltk
        # for competitor_check) must land alongside it in the /tmp target. We do skip
        # already-installed deps via --upgrade-strategy=only-if-needed so we don't
        # duplicate presidio/spacy that requirements.txt already put in site-packages.
        for spec in (f"{pkg}[validators]", pkg):
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--target", _HUB_TARGET,
                     "--upgrade-strategy", "only-if-needed",
                     "--index-url", index,
                     "--extra-index-url", "https://pypi.org/simple", "-q", spec],
                    capture_output=True, text=True, timeout=420, check=False,
                )
                if proc.returncode == 0:
                    installed = True
                    break
                last_err = (proc.stderr or proc.stdout or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)

        if not installed:
            errors.append(f"{key}: {last_err[-160:] or 'install failed'}")
            continue

        _ensure_nltk_data()
        importlib.invalidate_caches()
        try:
            _hub_module(key)
            status[key] = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: imported package but {type(exc).__name__}: {exc}")

    if errors:
        status["error"] = (
            "Some Hub validators are unavailable (regex/substring fallbacks active): "
            + " | ".join(errors)
        )
    return status


# ──────────────────────────────────────────────────────────────────────────────
# Custom validators — registered at import (pure Python, no Hub, no ML)
#   (a) function form  : refund_disclaimer
#   (b) class form     : MaxRefundClaim(threshold=…)
# Copied from notebook v3, cell 27.
# ──────────────────────────────────────────────────────────────────────────────
from guardrails.validators import (  # noqa: E402
    Validator,
    register_validator,
    PassResult,
    FailResult,
    ValidationResult,
)


@register_validator(name="nimbus/refund-disclaimer", data_type="string")
def refund_disclaimer(value: str, metadata: dict) -> ValidationResult:
    """Refund replies must carry 'subject to verification'."""
    if "refund" in value.lower() and "subject to verification" not in value.lower():
        return FailResult(
            error_message="Refund answer missing the 'subject to verification' disclaimer.",
            fix_value=value.rstrip(".") + ". All refunds are subject to verification.",
        )
    return PassResult()


@register_validator(name="nimbus/max-refund-claim", data_type="string")
class MaxRefundClaim(Validator):
    """Flag dollar amounts above `threshold` — those need human sign-off."""

    def __init__(self, threshold: float = 500.0, on_fail=None):
        super().__init__(on_fail=on_fail, threshold=threshold)
        self.threshold = threshold

    def validate(self, value: str, metadata: dict) -> ValidationResult:
        amounts = [
            float(a.replace(",", ""))
            for a in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)", value)
        ]
        over = [a for a in amounts if a > self.threshold]
        if over:
            return FailResult(
                error_message=(
                    f"Promised refund {over} exceeds ${self.threshold:.0f} "
                    "auto-approve cap — needs human sign-off."
                ),
            )
        return PassResult()


# ──────────────────────────────────────────────────────────────────────────────
# Guard builders — Hub validators (lazy import; only valid after setup)
# ──────────────────────────────────────────────────────────────────────────────
# NOTE: guardrails-ai 0.10.0's `Guard().use(*validators)` takes validator *instances*;
# params and `on_fail` go to the constructor (unlike the 0.6.x class+kwargs form used in
# the notebook). We instantiate here and pass the instance.
def pii_guard(on_fail):
    """Guard().use(DetectPII(...)). Raises ImportError if the Hub install failed."""
    from guardrails import Guard

    # Belt-and-braces: also pin here in case pii_guard is called before
    # setup_guardrails has run (e.g. from a cached resource path).
    _pin_presidio_to_small_spacy()
    DetectPII = _hub_module("pii")
    return Guard().use(DetectPII(pii_entities=PII_ENTITIES, on_fail=on_fail))


def competitor_guard(on_fail):
    """Guard().use(CompetitorCheck(...)). Raises ImportError if the Hub install failed."""
    from guardrails import Guard

    CompetitorCheck = _hub_module("competitor")
    return Guard().use(CompetitorCheck(competitors=COMPETITORS, on_fail=on_fail))


def disclaimer_guard(on_fail):
    from guardrails import Guard

    return Guard().use(refund_disclaimer(on_fail=on_fail))


def refund_cap_guard(threshold: float, on_fail):
    from guardrails import Guard

    return Guard().use(MaxRefundClaim(threshold=threshold, on_fail=on_fail))


# ──────────────────────────────────────────────────────────────────────────────
# Regex / substring fallbacks (used when Hub install is unavailable)
# ──────────────────────────────────────────────────────────────────────────────
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d -]{7,}\d)\b")


def regex_pii_scrub(text: str) -> tuple[str, bool]:
    """Anonymise PII with regex. Returns (scrubbed, found_any)."""
    found = False
    out = text
    for label, rx in [("<CREDIT_CARD>", _CARD_RE), ("<EMAIL_ADDRESS>", _EMAIL_RE),
                      ("<PHONE_NUMBER>", _PHONE_RE)]:
        new = rx.sub(label, out)
        if new != out:
            found = True
        out = new
    return out, found


def regex_pii_present(text: str) -> bool:
    return any(rx.search(text) for rx in (_CARD_RE, _EMAIL_RE, _PHONE_RE))


def substring_competitor_filter(text: str) -> tuple[str, bool]:
    """Drop sentences mentioning a competitor. Returns (filtered, found_any)."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept, found = [], False
    for s in sentences:
        if any(c.lower() in s.lower() for c in COMPETITORS):
            found = True
            continue
        kept.append(s)
    return " ".join(kept).strip(), found


# Sentence-level FILTER helpers. Hub validators (DetectPII, refund cap, etc.)
# return None for `on_fail=FILTER` on a string output — that's correct per
# Guardrails semantics (FILTER is designed for list-typed validators), but
# visually identical to REFRAIN, so the playground demo can't show the
# difference. We implement FILTER as "drop sentences that fail; keep the rest"
# so the BEFORE/AFTER pair is meaningful for the learner.
def filter_pii_sentences(text: str) -> tuple[str, bool]:
    """Drop sentences that contain PII; keep clean sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept, found = [], False
    for s in sentences:
        if regex_pii_present(s):
            found = True
            continue
        kept.append(s)
    return " ".join(kept).strip(), found


def filter_refund_sentences(text: str, threshold: float) -> tuple[str, bool]:
    """Drop sentences that mention a dollar amount above `threshold`."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept, found = [], False
    amount_re = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
    for s in sentences:
        over = any(
            float(a.replace(",", "")) > threshold for a in amount_re.findall(s)
        )
        if over:
            found = True
            continue
        kept.append(s)
    return " ".join(kept).strip(), found


# ──────────────────────────────────────────────────────────────────────────────
# LLM-as-judge (Groq) for toxicity & topic — no local ML weights
# ──────────────────────────────────────────────────────────────────────────────
def _judge(prompt: str) -> dict:
    """Call Groq in JSON mode and parse the result. Returns {} on failure."""
    import litellm

    try:
        resp = litellm.completion(
            model=BOT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


def judge_toxicity(text: str) -> dict:
    """LLM-as-judge toxicity check. Returns {flagged, score, reason}."""
    prompt = (
        "You are a content-safety classifier. Rate the toxicity of the TEXT.\n"
        "Return ONLY JSON: {\"flagged\": bool, \"score\": float 0..1, \"reason\": str}.\n"
        "Flag insults, harassment, hate, or abusive language.\n\n"
        f"TEXT:\n{text}"
    )
    out = _judge(prompt)
    return {
        "flagged": bool(out.get("flagged", False)),
        "score": float(out.get("score", 0.0) or 0.0),
        "reason": out.get("reason", out.get("_error", "")),
    }


def judge_topic(text: str, valid_topics: list[str], invalid_topics: list[str]) -> dict:
    """LLM-as-judge topic restriction. Returns {on_topic, reason}."""
    prompt = (
        "You are a topic classifier for a fintech support bot.\n"
        f"VALID topics: {', '.join(valid_topics)}.\n"
        f"INVALID topics: {', '.join(invalid_topics)}.\n"
        "Decide whether the TEXT stays on a valid topic.\n"
        "Return ONLY JSON: {\"on_topic\": bool, \"reason\": str}.\n\n"
        f"TEXT:\n{text}"
    )
    out = _judge(prompt)
    return {
        "on_topic": bool(out.get("on_topic", True)),
        "reason": out.get("reason", out.get("_error", "")),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Structured parsing — Pydantic models + Guard.for_pydantic (notebook cells 15, 34)
# ──────────────────────────────────────────────────────────────────────────────
from typing import Literal, Optional  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402


class SupportResponse(BaseModel):
    answer: str = Field(description="The reply shown to the customer.")
    category: Literal["refund", "account", "complaint", "other"] = Field(
        description="Ticket routing category."
    )
    needs_human: bool = Field(description="True if a human agent must follow up.")
    sentiment: Literal["positive", "neutral", "negative"] = Field(
        description="Customer's apparent sentiment."
    )


class TicketExtract(BaseModel):
    customer_issue: str = Field(description="One-sentence summary of the problem.")
    category: Literal["refund", "account", "complaint", "billing", "other"]
    refund_amount: Optional[float] = Field(
        None, description="Dollar amount requested as a refund, if stated."
    )
    urgency: Literal["low", "medium", "high"] = Field(
        description="Urgency based on tone and content."
    )
    needs_human: bool
    sentiment: Literal["positive", "neutral", "negative"]


def naive_bot(user_msg: str) -> str:
    """Plain Groq call — used by the Input Guard tab once a prompt passes."""
    import litellm

    resp = litellm.completion(
        model=BOT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content
