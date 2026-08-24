"""Fact extraction with provenance, plus the anti-fabrication number registry.

Every number that reaches a message body must first pass through this module.
On construction, ``Facts`` walks each pushed context payload and records every
number it finds — numeric leaves and numbers embedded in strings alike. A
composed body is then checked against that registry: any numeric token the
contexts never contained is a fabrication and the composer falls back.

Derived numbers (a percentage-point gap against peer median, days since a visit,
a formatted slot label) are legitimate but are not literally in the payload, so
whoever derives them calls :meth:`Facts.trust` on the rendered fragment. That
keeps the registry honest — text only becomes trusted by being derived from
context, never by being asserted.
"""

from __future__ import annotations

import re
from datetime import datetime

from .util import (as_list, day_delta, fmt_date_plain, fmt_day, fmt_slot, get_path,
                   grouped, humanise_slug, inr, month_matches_range, normalise,
                   pct, pct_points, to_number)

NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Structural integers that describe effort, choice count, or clock time rather
# than any claim about the merchant's business. Allowing these is what makes
# "2-min abstract", "Reply 1 / 2", and "takes 5 min" expressible at all.
EFFORT_SAFE = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 24, 30, 45, 48, 60, 90, 120}

# Digest kinds, most to least "must act on".
KIND_PRIORITY = ["compliance", "alert", "supply", "research", "tech", "trend",
                 "compete", "seasonal", "cde"]

# Aggregate keys that pair with a digest item's patient_segment.
SEGMENT_KEYS = {
    "high_risk_adults": "high_risk_adult_count",
    "chronic": "chronic_rx_count",
    "seniors": "chronic_rx_count",
    "lapsed": "lapsed_180d_plus",
}

# Stored slot preferences, phrased as the customer's own habit rather than as a
# field value. Bare weekday slugs are the trap this table exists for: a generic
# de-slugger turns ``"saturday"`` into "in the saturday" and
# ``"morning_delivery"`` into "in the morning delivery", both of which read as
# plumbing leaking into a sentence the customer is meant to recognise.
_SLOT_PHRASES = {
    "weekday_evening": "on weekday evenings",
    "weekday_morning": "on weekday mornings",
    "weekend_morning": "on weekend mornings",
    "weekend_evening": "on weekend evenings",
    "saturday": "on Saturdays",
    "sunday": "on Sundays",
    "saturday_morning": "on Saturday mornings",
    "saturday_evening": "on Saturday evenings",
    "sunday_morning": "on Sunday mornings",
    "morning_delivery": "in the morning",
    "evening_delivery": "in the evening",
    "afternoon_delivery": "in the afternoon",
    "fri_sat_night": "on Fri/Sat nights",
    "early_morning": "first thing in the morning",
    "late_evening": "late in the evening",
}

_WEEKDAY_WORDS = {"monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday", "mon", "tue", "wed", "thu", "fri",
                  "sat", "sun"}

_TIME_OF_DAY = {"morning", "afternoon", "evening", "night", "mornings",
                "afternoons", "evenings", "nights"}

_SIGNAL_DAYS = re.compile(r"[:_](\d+)\s*d$")


def parse_signal(raw) -> dict:
    """``"stale_posts:22d"`` -> ``{"raw":…, "name":"stale_posts", "days":22}``."""
    text = str(raw or "")
    match = _SIGNAL_DAYS.search(text)
    days = int(match.group(1)) if match else None
    name = text[:match.start()] if match else text
    return {"raw": text, "name": name.strip(":_"), "days": days}


def _walk_numbers(node, sink):
    """Collect every number reachable in a payload, including inside strings."""
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float)):
        sink.add(float(node))
        return
    if isinstance(node, str):
        for token in NUMBER_TOKEN.findall(node):
            value = to_number(token)
            if value is not None:
                sink.add(float(value))
        return
    if isinstance(node, dict):
        for key in sorted(node.keys(), key=str):
            _walk_numbers(node[key], sink)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk_numbers(item, sink)


class Facts:
    """Everything the composer is allowed to know about one (merchant, trigger)."""

    def __init__(self, category, merchant, trigger, customer=None, now=None,
                 category_slug=None):
        self.now: datetime = now
        self.category = category if isinstance(category, dict) else {}
        self.merchant = merchant if isinstance(merchant, dict) else {}
        self.trigger = trigger if isinstance(trigger, dict) else {}
        self.customer = customer if isinstance(customer, dict) else None
        self.category_slug = (category_slug or self.category.get("slug")
                              or self.merchant.get("category_slug") or "")

        self._numbers = set()
        for payload in (self.category, self.merchant, self.trigger, self.customer):
            if payload:
                _walk_numbers(payload, self._numbers)
        # A ratio in the payload is usually quoted as a percentage in prose.
        for value in list(self._numbers):
            if 0.0 < value < 1.0:
                self._numbers.add(round(value * 100, 4))
                self._numbers.add(round(value * 100, 1))
                self._numbers.add(float(round(value * 100)))
            if value > 1.0:
                self._numbers.add(round(value, 1))
                self._numbers.add(float(round(value)))
        self._numbers |= {float(n) for n in EFFORT_SAFE}

        self.provenance = []          # human-readable audit trail for rationale
        self._extract()

    # ------------------------------------------------------------------ registry
    def register(self, *values):
        for value in values:
            number = to_number(value)
            if number is None:
                continue
            number = float(number)
            self._numbers.add(number)
            self._numbers.add(round(number, 1))
            self._numbers.add(float(round(number)))
            if 0.0 < number < 1.0:
                self._numbers.add(round(number * 100, 1))
                self._numbers.add(float(round(number * 100)))

    def trust(self, text):
        """Register every number in a fragment derived from context, then return it."""
        if not text:
            return text
        for token in NUMBER_TOKEN.findall(str(text)):
            self.register(token)
        return text

    def unregistered_numbers(self, text):
        """Numeric tokens in ``text`` that no pushed context can account for."""
        stray = []
        for token in NUMBER_TOKEN.findall(str(text or "")):
            value = to_number(token)
            if value is None:
                continue
            value = float(value)
            if (value in self._numbers or round(value, 1) in self._numbers
                    or float(round(value)) in self._numbers):
                continue
            stray.append(token)
        return stray

    def note(self, label, value=None):
        self.provenance.append(label if value is None else f"{label}={value}")

    # ------------------------------------------------------------------ extraction
    def _extract(self):
        merchant, category = self.merchant, self.category
        ident = merchant.get("identity") or {}
        self.merchant_id = merchant.get("merchant_id") or self.trigger.get("merchant_id")
        self.business_name = ident.get("name") or ""
        self.owner_first = self._clean_first_name(ident.get("owner_first_name"))
        self.city = ident.get("city") or ""
        self.locality = ident.get("locality") or ""
        self.verified = bool(ident.get("verified"))
        self.languages = [str(x).lower() for x in as_list(ident.get("languages"))]
        self.established_year = ident.get("established_year")
        self.has_hindi = "hi" in self.languages

        self.voice = category.get("voice") or {}
        self.tone = self.voice.get("tone") or ""
        self.register_style = self.voice.get("register") or ""
        self.code_mix = self.voice.get("code_mix") or ""
        self.vocab_allowed = [str(v) for v in as_list(self.voice.get("vocab_allowed"))]
        self.vocab_taboo = [str(v) for v in as_list(self.voice.get("vocab_taboo"))]
        self.salutation = self._salutation()

        sub = merchant.get("subscription") or {}
        self.sub_status = (sub.get("status") or "").lower()
        self.plan = sub.get("plan")
        self.days_remaining = sub.get("days_remaining")
        self.days_since_expiry = sub.get("days_since_expiry")

        perf = merchant.get("performance") or {}
        self.window_days = perf.get("window_days")
        self.views = perf.get("views")
        self.calls = perf.get("calls")
        self.directions = perf.get("directions")
        self.leads = perf.get("leads")
        self.ctr = perf.get("ctr")
        delta = perf.get("delta_7d") or {}
        self.views_delta = delta.get("views_pct")
        self.calls_delta = delta.get("calls_pct")
        self.ctr_delta = delta.get("ctr_pct")

        self.peer = category.get("peer_stats") or {}
        self.peer_ctr = self.peer.get("avg_ctr")
        self.peer_views = self.peer.get("avg_views_30d")
        self.peer_calls = self.peer.get("avg_calls_30d")
        self.peer_post_days = self.peer.get("avg_post_freq_days")
        self.ctr_gap_points = None
        if isinstance(self.ctr, (int, float)) and isinstance(self.peer_ctr, (int, float)):
            self.ctr_gap_points = round((self.ctr - self.peer_ctr) * 100, 1)
            self.register(abs(self.ctr_gap_points), self.ctr * 100, self.peer_ctr * 100)
        self.views_vs_peer = None
        if (isinstance(self.views, (int, float)) and isinstance(self.peer_views, (int, float))
                and self.peer_views):
            self.views_vs_peer = round(self.views / self.peer_views, 2)

        self.aggregate = merchant.get("customer_aggregate") or {}
        self.signals = [parse_signal(s) for s in as_list(merchant.get("signals"))]
        self.signal_names = {s["name"] for s in self.signals}
        self.review_themes = [t for t in as_list(merchant.get("review_themes"))
                              if isinstance(t, dict)]
        self.history = [h for h in as_list(merchant.get("conversation_history"))
                        if isinstance(h, dict)]

        self.offers = [o for o in as_list(merchant.get("offers")) if isinstance(o, dict)]
        self.active_offers = [o for o in self.offers
                              if str(o.get("status", "")).lower() == "active"]
        self.offer_catalog = [o for o in as_list(category.get("offer_catalog"))
                              if isinstance(o, dict)]
        self.digest = [d for d in as_list(category.get("digest")) if isinstance(d, dict)]
        self.seasonal_beats = [b for b in as_list(category.get("seasonal_beats"))
                               if isinstance(b, dict)]
        self.trend_signals = [t for t in as_list(category.get("trend_signals"))
                              if isinstance(t, dict)]
        self.authorities = [str(a) for a in as_list(category.get("regulatory_authorities"))]
        self.journals = [str(j) for j in as_list(category.get("professional_journals"))]
        self.content_library = [c for c in as_list(category.get("patient_content_library"))
                                if isinstance(c, dict)]

        # ---- trigger
        self.kind = self.trigger.get("kind") or ""
        self.trigger_id = self.trigger.get("id")
        self.trigger_scope = (self.trigger.get("scope") or "merchant").lower()
        self.urgency = self.trigger.get("urgency")
        self.urgency_value = int(self.urgency) if isinstance(self.urgency, (int, float)) else 2
        raw_payload = self.trigger.get("payload")
        self.tpayload = raw_payload if isinstance(raw_payload, dict) else {}
        self.placeholder_payload = bool(self.tpayload.get("placeholder"))
        self.suppression_key = self.trigger.get("suppression_key")
        self.expires_at = self.trigger.get("expires_at")

        # ---- customer
        cust = self.customer or {}
        cident = cust.get("identity") or {}
        crel = cust.get("relationship") or {}
        cprefs = cust.get("preferences") or {}
        cconsent = cust.get("consent") or {}
        self.customer_id = cust.get("customer_id") or self.trigger.get("customer_id")
        self.customer_name = self._clean_customer_name(cident.get("name"))
        self.parent_name = self._parent_name(cident.get("name"))
        self.language_pref = (cident.get("language_pref") or "").lower()
        self.age_band = cident.get("age_band")
        self.senior = bool(cident.get("senior_citizen"))
        self.customer_state = (cust.get("state") or "").lower()
        self.first_visit = crel.get("first_visit")
        self.last_visit = crel.get("last_visit")
        self.visits_total = crel.get("visits_total")
        self.services_received = [str(s) for s in as_list(crel.get("services_received"))]
        self.chronic_conditions = [str(c) for c in as_list(crel.get("chronic_conditions"))]
        self.favourite_dish = crel.get("favourite_dish")
        self.lifetime_value = crel.get("lifetime_value")
        self.channel = cprefs.get("channel")
        self.via_parent = "parent" in str(self.channel or "").lower()
        self.via_proxy = "via_" in str(self.channel or "").lower()
        self.reminder_opt_in = cprefs.get("reminder_opt_in")
        self.preferred_slots = cprefs.get("preferred_slots")
        self.preferred_stylist = cprefs.get("preferred_stylist")
        self.training_focus = cprefs.get("training_focus")
        self.health_focus = cprefs.get("health_focus")
        self.wedding_date = cprefs.get("wedding_date")
        self.address_saved = str(cprefs.get("delivery_address") or "").lower() == "saved"
        self.office_nearby = bool(cprefs.get("office_nearby"))
        self.household_size = cprefs.get("household_size") or cprefs.get("family_size")
        self.consent_scope = [str(s) for s in as_list(cconsent.get("scope"))]
        self.has_customer = bool(self.customer)
        self.customer_consented = bool(self.consent_scope) and self.reminder_opt_in is not False
        self.wants_hinglish = ("hi" in self.language_pref) or (
            not self.language_pref and self.has_hindi)

        self.days_since_visit = None
        if self.last_visit and self.now:
            delta_days = day_delta(self.last_visit, self.now)
            if delta_days is not None and delta_days >= 0:
                self.days_since_visit = delta_days
                self.register(delta_days, delta_days // 7, delta_days // 30)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _clean_first_name(raw):
        name = str(raw or "").strip()
        if not name:
            return ""
        for prefix in ("Dr. ", "Dr.", "Dr "):
            if name.startswith(prefix):
                return name[len(prefix):].strip()
        return name

    def _salutation(self):
        """Fill a category salutation template with what identity actually gives us."""
        ident = self.merchant.get("identity") or {}
        first = self._clean_first_name(ident.get("owner_first_name"))
        business = ident.get("name") or ""
        for template in as_list(self.voice.get("salutation_examples")):
            text = str(template)
            if "{" not in text:
                continue
            if ("first_name" in text or "chef_or_owner_first_name" in text
                    or "pharmacist_name" in text):
                if not first:
                    continue
                return (text.replace("{chef_or_owner_first_name}", first)
                            .replace("{pharmacist_name}", first)
                            .replace("{first_name}", first))
            if business:
                return re.sub(r"\{[a-z_]+\}", business, text)
        if first:
            return f"Dr. {first}" if self.category_slug == "dentists" else f"Hi {first}"
        return f"{business} team" if business else "Hi"

    def name_or_team(self):
        return self.owner_first or (f"{self.business_name} team" if self.business_name else "")

    @staticmethod
    def _clean_customer_name(raw):
        """The name to actually address, or ``None`` when there is no profile.

        Seeds carry four shapes: ``"Amit"``, ``"Mr. Sharma"``,
        ``"Karthik (parent: Sumitra)"`` and ``"(walk-in, no profile)"``. Splitting
        on whitespace alone turns the second into "Mr.", so honorifics keep their
        surname; the last is a placeholder, not a name, and using it as a
        salutation would read worse than no salutation at all.
        """
        name = str(raw or "").strip()
        if not name or name.startswith("("):
            return None
        name = name.split(" (")[0].strip()
        words = name.split()
        if not words:
            return None
        honorifics = {"mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.",
                      "shri", "smt", "smt.", "sri"}
        if words[0].lower() in honorifics and len(words) > 1:
            return f"{words[0]} {words[1]}"
        return words[0]

    @staticmethod
    def _parent_name(raw):
        """The guardian named in ``"Aanya (parent: Sneha)"``, if any."""
        match = re.search(r"\(\s*parent\s*:\s*([^)]+)\)", str(raw or ""), re.I)
        return match.group(1).strip() or None if match else None

    def preference_clause(self):
        """A short phrase proving we remember this customer, or ``""``.

        Only ever built from stored preferences — never guessed. Slot strings are
        turned into habits (``"fri_sat_night"`` -> ``"on Fri/Sat nights"``,
        ``"saturday"`` -> ``"on Saturdays"``) so the customer reads their own
        routine back rather than our field value.
        """
        if self.preferred_stylist:
            return f"with {self.preferred_stylist}"
        slots = self.preferred_slots
        if not slots:
            return ""
        lowered = str(slots).strip().lower()
        known = _SLOT_PHRASES.get(lowered)
        if known:
            return known
        label = humanise_slug(slots)
        if lowered.endswith(("am", "pm")):
            return f"around {label}"

        words = label.lower().split()
        # "saturday morning" -> "on Saturday mornings"; "saturday" -> "on Saturdays".
        if words and words[0] in _WEEKDAY_WORDS:
            day = words[0].capitalize()
            if len(words) > 1 and words[-1] in _TIME_OF_DAY:
                part = words[-1].rstrip("s") + "s"
                return f"on {day} {part}"
            return f"on {day}s"
        if words and words[-1] in _TIME_OF_DAY:
            return f"in the {' '.join(words)}"
        return f"for {label.lower()}"

    def focus_clause(self):
        """What this customer was working on, in their own recorded terms."""
        for value in (self.training_focus, self.health_focus):
            if value:
                return humanise_slug(value).lower()
        if self.chronic_conditions:
            return humanise_slug(self.chronic_conditions[0]).lower()
        if self.services_received:
            return humanise_slug(self.services_received[-1]).lower()
        return ""

    # ------------------------------------------------------------------ digest
    def digest_item(self, item_id):
        for item in self.digest:
            if item.get("id") == item_id:
                return item
        return None

    def digest_by_kind(self, *kinds):
        wanted = {k.lower() for k in kinds}
        return [d for d in self.digest if str(d.get("kind", "")).lower() in wanted]

    def digest_relevance(self, item) -> int:
        """How well a digest item fits *this* merchant. Deterministic integer score."""
        score = 0
        segment = str(item.get("patient_segment") or "")
        aggregate_key = SEGMENT_KEYS.get(segment)
        if aggregate_key and to_number(self.aggregate.get(aggregate_key)):
            score += 4
        kind = str(item.get("kind", "")).lower()
        if kind in ("compliance", "alert", "supply"):
            score += 5
        if kind == "research" and "high_risk_adult_cohort" in self.signal_names:
            score += 2
        if kind == "trend" and "ctr_below_peer_median" in self.signal_names:
            score += 2
        if kind == "tech" and self.sub_status == "active":
            score += 1
        haystack = normalise(f"{item.get('title','')} {item.get('summary','')}")
        for term in self.vocab_allowed:
            token = normalise(term)
            if token and token in haystack:
                score += 1
                break
        for theme in self.review_themes:
            label = normalise(theme.get("theme"))
            if label and label.split()[0] in haystack:
                score += 2
                break
        return score

    def best_digest(self, prefer_kinds=None, exclude_ids=()):
        """Pick the single most relevant digest item, ties broken by id for stability."""
        pool = [d for d in self.digest if d.get("id") not in set(exclude_ids)]
        if prefer_kinds:
            wanted = {k.lower() for k in prefer_kinds}
            preferred = [d for d in pool if str(d.get("kind", "")).lower() in wanted]
            pool = preferred or pool
        if not pool:
            return None

        def sort_key(item):
            kind = str(item.get("kind", "")).lower()
            priority = KIND_PRIORITY.index(kind) if kind in KIND_PRIORITY else len(KIND_PRIORITY)
            return (-self.digest_relevance(item), priority, str(item.get("id", "")))

        return sorted(pool, key=sort_key)[0]

    def citation(self, item):
        """The source string a research/compliance claim must carry."""
        if not isinstance(item, dict):
            return None
        source = item.get("source")
        return self.trust(str(source)) if source else None

    # ------------------------------------------------------------------ offers
    def offer_for(self, audience=None, exclude_titles=()):
        """Prefer the merchant's own live offer; fall back to the category catalog."""
        skip = {normalise(t) for t in exclude_titles}
        for offer in self.active_offers:
            title = offer.get("title")
            if title and normalise(title) not in skip:
                self.trust(title)
                return {"title": str(title), "id": offer.get("id"), "origin": "merchant_offer"}
        pool = self.offer_catalog
        if audience:
            matched = [o for o in pool if str(o.get("audience", "")) == audience]
            pool = matched or pool
        for offer in pool:
            title = offer.get("title")
            if title and normalise(title) not in skip:
                self.trust(title)
                return {"title": str(title), "id": offer.get("id"),
                        "origin": "category_catalog"}
        return None

    def offer_price(self, offer):
        if not isinstance(offer, dict):
            return None
        for candidate in self.offer_catalog:
            if candidate.get("id") == offer.get("id"):
                value = to_number(candidate.get("value"))
                if value is not None:
                    self.register(value)
                    return value
        value = to_number(offer.get("value"))
        if value is not None:
            self.register(value)
        return value

    # ------------------------------------------------------------------ seasonality
    def seasonal_note(self):
        for beat in self.seasonal_beats:
            if month_matches_range(beat.get("month_range"), self.now):
                self.trust(beat.get("note"))
                return {"note": str(beat.get("note") or ""),
                        "months": str(beat.get("month_range") or "")}
        return None

    def top_trend(self):
        ranked = sorted(
            [t for t in self.trend_signals if to_number(t.get("delta_yoy")) is not None],
            key=lambda t: (-float(to_number(t.get("delta_yoy"))), str(t.get("query", ""))))
        if not ranked:
            return None
        top = ranked[0]
        delta = float(to_number(top.get("delta_yoy")))
        self.register(delta)
        return {"query": str(top.get("query") or ""), "delta": delta,
                "segment": top.get("segment_age"), "skew": top.get("skew")}

    # ------------------------------------------------------------------ narrative bits
    def aggregate_anchor(self):
        """The biggest honest number about this merchant's own customer base."""
        preferred = ["high_risk_adult_count", "chronic_rx_count", "total_active_members",
                     "lapsed_180d_plus", "lapsed_90d_plus", "delivery_orders_30d",
                     "dine_in_orders_30d", "total_unique_ytd"]
        labels = {
            "high_risk_adult_count": ("high-risk adult patients", "patients flagged high-risk"),
            "chronic_rx_count": ("chronic-Rx customers", "customers on repeat prescriptions"),
            "total_active_members": ("active members", "members on the roster"),
            "lapsed_180d_plus": ("customers lapsed 6 months or more", "long-lapsed customers"),
            "lapsed_90d_plus": ("customers lapsed 3 months or more", "lapsed customers"),
            "delivery_orders_30d": ("delivery orders last month", "delivery orders"),
            "dine_in_orders_30d": ("dine-in orders last month", "dine-in orders"),
            "total_unique_ytd": ("unique customers this year", "customers on record"),
        }
        for key in preferred:
            value = to_number(self.aggregate.get(key))
            if value:
                self.register(value)
                short, long_label = labels.get(key, (humanise_slug(key), humanise_slug(key)))
                return {"key": key, "value": int(value), "label": short,
                        "long_label": long_label, "text": f"{grouped(int(value))} {short}"}
        return None

    def strongest_signal(self):
        """Rank merchant signals so a sparse trigger still has something concrete.

        Weights encode "what would I raise first if I ran this business" — supply
        and regulation before growth, growth before vanity. Signals that are good
        news (``high_retention``, ``above_peer_ctr``) sort last: they are context
        for a message, never the reason for one.
        """
        weight = {
            # blocking / external
            "supply_alert_affected": 9, "regulation_pending": 9,
            # money at risk
            "perf_dip_severe": 8, "active_planning": 8, "perf_dip_post_expiry": 8,
            "subscription_expired": 7, "renewal_due_soon": 7, "trial_ending_soon": 7,
            "unverified_gbp": 7, "trial_ending": 6,
            # fixable neglect
            "ctr_below_peer_median": 6, "stale_posts": 6, "no_active_offers": 6,
            "high_churn_risk": 6, "review_theme_negative": 6, "no_recent_post": 5,
            "delivery_not_set_up": 5, "no_recent_conversation": 4,
            # cohorts worth working
            "winback_eligible": 5, "lapsed_cohort_large": 5, "seasonal_dip_apr_may": 6,
            "high_risk_adult_cohort": 5, "chronic_rx_cohort": 5, "new_merchant": 5,
            "dormant_with_vera": 4, "engaged_in_last_48h": 4, "engaged_in_last_24h": 4,
            "ipl_eligible_locality": 3,
            # good news: never the reason to interrupt someone
            "high_engagement": 1, "high_retention": 1, "high_repeat_rate": 1,
            "above_peer_median_calls": 1, "above_peer_ctr": 1, "above_peer_calls": 1,
            "growing_views": 1, "growing_views_7d": 1, "high_volume": 1,
            "stable_growth": 1, "boutique_segment": 1, "compliance_aware": 1,
        }
        ranked = sorted(self.signals, key=lambda s: (-weight.get(s["name"], 2), s["raw"]))
        return ranked[0] if ranked else None

    def performance_story(self):
        """Which metric moved, by how much, and whether that is good news."""
        candidates = []
        if isinstance(self.views_delta, (int, float)):
            candidates.append(("views", float(self.views_delta), self.views))
        if isinstance(self.calls_delta, (int, float)):
            candidates.append(("calls", float(self.calls_delta), self.calls))
        if not candidates:
            return None
        metric, delta, absolute = max(candidates, key=lambda c: (abs(c[1]), c[0]))
        self.register(abs(delta), abs(delta) * 100)
        return {"metric": metric, "delta": delta, "absolute": absolute,
                "direction": "down" if delta < 0 else "up",
                "delta_text": self.trust(pct_points(delta)),
                "absolute_text": self.trust(grouped(absolute)) if absolute is not None else None}

    def last_merchant_turn(self):
        """Most recent merchant message, for grounding a resumed thread."""
        for turn in reversed(self.history):
            if str(turn.get("from", "")).lower() == "merchant":
                return turn
        return None

    def strongest_review_theme(self):
        """Negative themes first — the seeds spell sentiment ``"neg"``/``"pos"``."""
        ranked = sorted(
            [t for t in self.review_themes if t.get("theme")],
            key=lambda t: (0 if str(t.get("sentiment", "")).lower().startswith("neg") else 1,
                           -(to_number(t.get("occurrences_30d")) or 0), str(t.get("theme"))))
        if not ranked:
            return None
        theme = ranked[0]
        count = to_number(theme.get("occurrences_30d"))
        if count:
            self.register(count)
        return {"theme": str(theme.get("theme")), "sentiment": theme.get("sentiment"),
                "count": int(count) if count else None,
                "quote": theme.get("common_quote")}

    # ------------------------------------------------------------------ trigger payload
    def payload_slots(self):
        """Normalised ``available_slots`` from a booking-shaped trigger."""
        slots = []
        for raw in as_list(self.tpayload.get("available_slots")):
            if isinstance(raw, dict):
                iso, label = raw.get("iso"), raw.get("label")
            else:
                iso, label = raw, None
            text = label or (fmt_slot(iso) if iso else None)
            if text:
                slots.append(self.trust(str(text)))
        return slots

    def payload_number(self, *keys):
        for key in keys:
            value = to_number(self.tpayload.get(key))
            if value is not None:
                self.register(value)
                return value
        return None

    def payload_text(self, *keys):
        for key in keys:
            value = self.tpayload.get(key)
            if isinstance(value, str) and value.strip():
                return self.trust(value.strip())
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.register(value)
                return grouped(value)
        return None

    def payload_day(self, *keys):
        for key in keys:
            value = self.tpayload.get(key)
            if value:
                text = fmt_day(value)
                if text:
                    return self.trust(text)
        return None

    def payload_slot_label(self, *keys):
        for key in keys:
            value = self.tpayload.get(key)
            if value:
                text = fmt_slot(value)
                if text:
                    return self.trust(text)
        return None

    def payload_date_plain(self, *keys):
        for key in keys:
            value = self.tpayload.get(key)
            if value:
                text = fmt_date_plain(value)
                if text:
                    return self.trust(text)
        return None

    def payload_list(self, *keys):
        for key in keys:
            values = as_list(self.tpayload.get(key))
            if values:
                return [self.trust(str(v)) for v in values if v is not None]
        return []

    def days_until(self, *keys):
        for key in keys:
            value = self.tpayload.get(key)
            if not value:
                continue
            delta = day_delta(value, self.now)
            if delta is not None:
                self.register(abs(delta))
                return delta
        return None

    # ------------------------------------------------------------------ formatting
    def money(self, amount):
        value = to_number(amount)
        if value is None:
            return None
        self.register(value)
        return inr(value)

    def count(self, amount):
        value = to_number(amount)
        if value is None:
            return None
        self.register(value)
        return grouped(value)

    def percent(self, fraction):
        value = to_number(fraction)
        if value is None:
            return None
        self.register(value, abs(value) * 100)
        return pct(value)


def build_facts(store, trigger, now, merchant_id=None, customer_id=None) -> Facts:
    """Assemble a ``Facts`` view from whatever the judge has actually pushed."""
    trigger = trigger if isinstance(trigger, dict) else {}
    merchant_id = merchant_id or trigger.get("merchant_id")
    customer_id = customer_id or trigger.get("customer_id")
    merchant = store.get("merchant", merchant_id) or {}
    slug = merchant.get("category_slug") or get_path(trigger, "payload", "category")
    category = store.get("category", slug) or {}
    customer = store.get("customer", customer_id) if customer_id else None
    return Facts(category=category, merchant=merchant, trigger=trigger, customer=customer,
                 now=now, category_slug=slug)
