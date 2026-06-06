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

import json
import os
import re
import subprocess
import sys

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


def configure_keys() -> dict:
    """Push keys into the environment so litellm / guardrails pick them up.

    Returns a dict of which keys are present.
    """
    groq = get_key("GROQ_API_KEY")
    grd = get_key("GUARDRAILS_TOKEN")
    if groq:
        os.environ["GROQ_API_KEY"] = groq
    if grd:
        os.environ["GUARDRAILS_TOKEN"] = grd
    return {"groq": bool(groq), "guardrails": bool(grd)}


# ──────────────────────────────────────────────────────────────────────────────
# One-time Guardrails setup: configure + hub install (cached per cold start)
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner="Configuring Guardrails & installing Hub validators…")
def setup_guardrails() -> dict:
    """Configure the Hub CLI and install the two light validators.

    Returns a status dict: {"pii": bool, "competitor": bool, "error": str | None}.
    On any failure the caller falls back to regex / substring matching, so the app
    never hard-fails on Streamlit Cloud.
    """
    status = {"pii": False, "competitor": False, "error": None}
    token = os.environ.get("GUARDRAILS_TOKEN")

    if not token:
        status["error"] = "No GUARDRAILS_TOKEN — using regex/substring fallbacks."
        return status

    def run(cmd: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, check=False
            )
            return proc.returncode == 0, (proc.stderr or proc.stdout)[-500:]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # 1. Configure the Hub CLI non-interactively.
    ok, msg = run(
        ["guardrails", "configure", "--token", token, "--disable-metrics",
         "--disable-remote-inferencing"]
    )
    if not ok:
        status["error"] = f"`guardrails configure` failed: {msg}"
        return status

    # 2. Install the two light validators.
    ok_pii, msg_pii = run(
        ["guardrails", "hub", "install", "hub://guardrails/detect_pii", "--quiet"]
    )
    status["pii"] = ok_pii

    ok_comp, msg_comp = run(
        ["guardrails", "hub", "install", "hub://guardrails/competitor_check", "--quiet"]
    )
    status["competitor"] = ok_comp

    if not (ok_pii and ok_comp):
        status["error"] = (
            "Some Hub installs failed (fallbacks active). "
            f"pii={msg_pii[-160:]!r} competitor={msg_comp[-160:]!r}"
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
    from guardrails.hub import DetectPII

    return Guard().use(DetectPII(pii_entities=PII_ENTITIES, on_fail=on_fail))


def competitor_guard(on_fail):
    """Guard().use(CompetitorCheck(...)). Raises ImportError if the Hub install failed."""
    from guardrails import Guard
    from guardrails.hub import CompetitorCheck

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
