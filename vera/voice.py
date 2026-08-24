"""Category voice: salutation, code-mix, emoji restraint, taboo and jargon scrubbing.

Two failure modes this module exists to prevent:

1. **Wrong register.** ``CategoryContext.voice`` is explicit about tone, allowed
   vocabulary, and forbidden claims. A body that ignores it reads like a mail
   merge, which is exactly what the challenge penalises.
2. **Internal vocabulary leaking out.** Signal slugs (``ctr_below_peer_median``),
   trigger kinds (``perf_dip``), context ids (``m_001_…``, ``d_2026W17_…``) and
   contract enums (``merchant_on_behalf``) are plumbing. A merchant reading them
   in a WhatsApp message immediately knows they are talking to a template.
"""

from __future__ import annotations

import re

from .util import as_list, join_sentences, normalise, squash

# Internal identifiers, in the shapes this dataset actually uses.
_ID_PATTERNS = [
    re.compile(r"\bm_\d{3}[a-z0-9_]*", re.I),          # merchant ids
    re.compile(r"\bc_\d{3}[a-z0-9_]*", re.I),          # customer ids
    re.compile(r"\btrg_\d{3}[a-z0-9_]*", re.I),        # trigger ids
    re.compile(r"\bd_\d{4}W\d{2}[a-z0-9_]*", re.I),    # digest item ids
    re.compile(r"\b(?:den|sal|res|gym|pha)_\d{3}\b", re.I),   # offer catalog ids
    re.compile(r"\bo_[a-z0-9]+_[a-z0-9]+\b", re.I),    # merchant offer ids
    re.compile(r"\bpc_[a-z0-9_]+\b", re.I),            # content-library ids
    re.compile(r"\bChIJ_[A-Z0-9_]+\b"),                # place ids
    re.compile(r"\b[a-z]+(?:_[a-z0-9]+){2,}\b"),       # any 3-part snake_case slug
]

# Contract enums and field names that must never reach a body.
_JARGON = [
    "merchant_on_behalf", "send_as", "suppression_key", "binary_yes_no",
    "binary_confirm_cancel", "multi_choice_slot", "open_ended", "template_name",
    "template_params", "customer_aggregate", "delta_7d", "conversation_id",
    "trigger_id", "available_triggers", "context_id", "merchant_id", "customer_id",
    "category_slug", "peer_stats", "trend_signals", "seasonal_beats", "offer_catalog",
]

# Internal phrases that read as machine output, with plain-language replacements.
_JARGON_PHRASES = [
    ("peer median", "the category average"),
    ("data point", "number"),
    ("high urgency", "time-sensitive"),
]

_CATEGORY_EMOJI = {
    "dentists": "\U0001F9B7",      # tooth
    "gyms": "\U0001F4AA",          # flexed biceps
    "pharmacies": "\U0001F48A",    # pill
    "restaurants": "\U0001F37D",   # fork and knife with plate
    "salons": "✨",            # sparkles
}

# Softer substitutes for the claims categories forbid. Keys are normalised taboo
# terms; anything not listed is dropped rather than reworded.
_TABOO_SOFTENERS = {
    "guaranteed": "typically",
    "100 safe": "well tolerated",
    "completely cure": "help manage",
    "miracle": "notable",
    "best in city": "well reviewed locally",
    "doctor approved": "clinically reviewed",
    "cures": "helps manage",
    "no side effects": "generally well tolerated",
    "permanent results": "long-lasting results",
    "instant": "quick",
    "detox": "reset",
}


def taboo_terms(raw_list) -> list:
    """Category taboo entries, stripped of the parenthetical guidance some carry."""
    terms = []
    for entry in as_list(raw_list):
        text = str(entry).split(" (")[0].strip()
        if text:
            terms.append(text)
    return terms


def _looks_hinglish(text) -> bool:
    """Cheap detector for romanised Hindi in a merchant's own message."""
    markers = {"hain", "hai", "kar", "karo", "nahi", "kitna", "kaise", "chahiye",
               "aap", "apke", "mera", "meri", "bhej", "batao", "theek", "acha",
               "kal", "abhi", "haan"}
    return bool(markers & set(normalise(text).split()))


class Voice:
    """Renders text in one category's register for one audience."""

    def __init__(self, facts):
        self.facts = facts
        self.slug = facts.category_slug or ""
        self.audience = "customer" if (facts.trigger_scope == "customer"
                                       or facts.customer_id) else "merchant"
        self.taboo = taboo_terms(facts.vocab_taboo)
        self.code_mix_ok = "hindi" in str(facts.code_mix).lower()

    # ------------------------------------------------------------------ openers
    def open(self) -> str:
        """The greeting. Named counterparty when we have one, never a bare 'Hi'.

        Three dataset facts change who is addressed and how:

        * ``channel: "whatsapp_via_parent"`` means the phone belongs to the
          guardian — greet them, and refer to the child in the third person.
        * a senior citizen, or anyone whose ``language_pref`` starts ``hi``, reads
          "Namaste" as courtesy rather than affectation.
        * ``"(walk-in, no profile)"`` yields no name at all, so we open plainly
          instead of inventing one.
        """
        if self.audience != "customer":
            return self.facts.salutation or "Hi"
        facts = self.facts
        if facts.via_parent and facts.parent_name:
            return f"Hi {facts.parent_name}"
        name = facts.customer_name
        if not name:
            return "Hi"
        formal = facts.senior or str(facts.language_pref or "").startswith("hi")
        return f"{'Namaste' if formal else 'Hi'} {name}"

    def merchant_intro(self) -> str:
        """Who is speaking, for customer-facing sends from the merchant's number."""
        business = self.facts.business_name
        owner = self.facts.owner_first
        if business and owner and self.slug != "dentists":
            return f"{owner} from {business} here"
        if business:
            return f"{business} here"
        return ""

    def emoji(self) -> str:
        """One category emoji, customer-facing only. Merchants get plain text.

        Suppressed when the reader is a guardian or the subject is a child: a 💪
        sent to a parent about a seven-year-old's yoga class reads as a mismatch,
        and a 💊 to a senior's family number reads as flippant.
        """
        facts = self.facts
        if self.audience != "customer":
            return ""
        if facts.via_parent or str(facts.age_band or "").lower().startswith("child"):
            return ""
        if facts.senior or facts.via_proxy:
            return ""
        return _CATEGORY_EMOJI.get(self.slug, "")

    # ------------------------------------------------------------------ code-mix
    def hinglish_ok(self) -> bool:
        """Only code-mix when both the category voice and the reader support it."""
        if not self.code_mix_ok:
            return False
        if self.audience == "customer":
            pref = self.facts.language_pref
            return ("hi" in pref) if pref else self.facts.has_hindi
        # Merchant-facing: the anchor cases stay in English. Code-mix only when the
        # merchant writes Hindi themselves.
        turn = self.facts.last_merchant_turn()
        return bool(turn and _looks_hinglish(turn.get("body")))

    def mix(self, english: str, hinglish: str) -> str:
        """Pick the Hinglish phrasing when appropriate, else plain English."""
        return hinglish if (hinglish and self.hinglish_ok()) else english

    # ------------------------------------------------------------------ vocabulary
    def domain_term(self, *candidates):
        """Return the first candidate the category's allowed vocabulary endorses."""
        allowed = {normalise(v) for v in self.facts.vocab_allowed}
        for candidate in candidates:
            if normalise(candidate) in allowed:
                return candidate
        return candidates[0] if candidates else ""

    def taboo_hits(self, text) -> list:
        """Forbidden claims present in ``text``, matched on word boundaries."""
        haystack = f" {normalise(text)} "
        return [term for term in self.taboo
                if normalise(term) and f" {normalise(term)} " in haystack]

    def jargon_hits(self, text) -> list:
        """Internal identifiers or contract enums present in ``text``."""
        raw = str(text or "")
        hits = []
        for pattern in _ID_PATTERNS:
            hits.extend(pattern.findall(raw))
        lowered = raw.lower()
        hits.extend(token for token in _JARGON if token in lowered)
        return sorted(set(hits))

    def scrub(self, text) -> str:
        """Remove internal identifiers and soften forbidden claims.

        A safety net, not the primary path — playbooks are written not to need it.
        When it fires, ``validate`` records the rewrite so the rationale stays
        truthful about what the merchant actually received.
        """
        out = str(text or "")
        for pattern in _ID_PATTERNS:
            out = pattern.sub("", out)
        for token in _JARGON:
            out = re.sub(re.escape(token), "", out, flags=re.I)
        for phrase, replacement in _JARGON_PHRASES:
            out = re.sub(re.escape(phrase), replacement, out, flags=re.I)
        for term in self.taboo:
            softener = _TABOO_SOFTENERS.get(normalise(term), "")
            out = re.sub(r"\b" + re.escape(term) + r"\b", softener, out, flags=re.I)
        out = re.sub(r"\s+([,.;:!?])", r"\1", out)
        out = re.sub(r"([,;:])\s*([,.;:])", r"\1", out)
        return squash(out)

    # ------------------------------------------------------------------ assembly
    def assemble(self, parts) -> str:
        """Join sentence fragments into one body, preserving deliberate breaks."""
        return join_sentences([p for p in parts if p])
