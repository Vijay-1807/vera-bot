"""One playbook per trigger kind: the actual judgment about what to say.

Design rules, all of them load-bearing for how this gets scored:

1. **Every number comes from a pushed context.** Anything derived (a peer gap, a
   renewal date, "12 calls down to 6") is passed through ``facts.trust`` at the
   point of derivation so the validator can tell derivation from invention.
2. **Playbooks may recommend silence.** Returning ``defer=True`` with a reason is
   a first-class outcome, not a failure. A Diwali message 188 days before Diwali
   scores worse than no message at all.
3. **State what we cannot see.** The interesting cases are the ones where the
   obvious message needs a number nobody pushed — which customers received a
   recalled batch, what a corporate thali should cost per head. Naming the gap and
   asking for it beats inventing a plausible figure.
4. **One ask per message.** The CTA vocabulary is fixed by the contract; the body
   must contain exactly one question, and it must map to the CTA we declare.

Each playbook takes ``(facts, voice)`` and returns a draft dict, or ``None`` to
fall through to the generic handler.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .facts import SEGMENT_KEYS
from .util import (first_clause, fmt_date_plain, fmt_day, fmt_slot, grouped,
                   humanise_slug, inr, pct_points, to_number)

# ---------------------------------------------------------------- contract enums

OPEN = "open_ended"
YES_NO = "binary_yes_no"
CONFIRM = "binary_confirm_cancel"
SLOTS = "multi_choice_slot"
NONE = "none"

VERA = "vera"
ON_BEHALF = "merchant_on_behalf"

_SPELLED = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty", 21: "twenty-one", 22: "twenty-two",
    23: "twenty-three", 24: "twenty-four", 30: "thirty", 38: "thirty-eight",
    45: "forty-five", 57: "fifty-seven", 60: "sixty",
}


def _spell(value):
    """Words for incidental counts, numerals for facts that carry weight.

    Specificity is scored, so a number that came from the merchant's own context
    stays a numeral — "38% lower recurrence" reads as evidence, "just over a
    third" reads as hedging. Only effort and duration asides get spelled out,
    where a digit would make the sentence look machine-filled.
    """
    number = to_number(value)
    if number is None:
        return None
    integer = int(round(number))
    return _SPELLED.get(integer) or grouped(integer)


def draft(body, cta=OPEN, send_as=VERA, topic="", why=(), citation=None,
          defer=False, defer_reason="", pending_ask="", value=5, audience=None,
          template=None, params=()):
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "topic": topic,
        "why": [w for w in why if w],
        "citation": citation,
        "defer": defer,
        "defer_reason": defer_reason,
        "pending_ask": pending_ask,
        "value": value,
        "audience": audience,
        "template_name": template,
        "template_params": [str(p) for p in params if p not in (None, "")],
    }


def hold(reason, topic="", why=(), value=0):
    """A deliberate silence. ``decide`` turns this into a hold carrying this reason."""
    return draft("", cta=NONE, topic=topic, why=why, defer=True,
                 defer_reason=reason, value=value)


# ---------------------------------------------------------------- shared fragments

def _greet(v):
    return v.open()


def _hello(f, v):
    """Customer-facing opener: greeting, one emoji, then who is speaking."""
    emoji = v.emoji()
    intro = v.merchant_intro()
    head = _greet(v) + (f" {emoji}" if emoji else "")
    return f"{head} {intro}." if intro else f"{head}."


def _title(f, item):
    return f.trust(str((item or {}).get("title") or "")).rstrip(". ")


def _evidence(f, item, max_words=24):
    return f.trust(first_clause((item or {}).get("summary"), max_words))


def _todo(f, item):
    """The dataset's own ``actionable`` line, lower-cased to sit mid-sentence."""
    text = f.trust(str((item or {}).get("actionable") or "")).strip().rstrip(".")
    return text[:1].lower() + text[1:] if text else ""


def _segment_tie(f, item):
    """Tie a digest item's ``patient_segment`` to this merchant's own headcount.

    The single highest-value join in the dataset: the dentists research item is
    about ``high_risk_adults`` and Dr. Meera's profile carries
    ``high_risk_adult_count``. Quoting her own number is the difference between a
    newsletter and advice.
    """
    key = SEGMENT_KEYS.get(str((item or {}).get("patient_segment") or ""))
    if not key:
        return None
    value = to_number(f.aggregate.get(key))
    if not value:
        return None
    f.register(value)
    label = {"high_risk_adult_count": "patients already flagged high-risk",
             "chronic_rx_count": "customers on repeat prescriptions",
             "lapsed_180d_plus": "customers lapsed six months or more"}.get(
                 key, humanise_slug(key))
    return f"{grouped(int(value))} {label}"


# Signal -> (what is actually wrong, what Vera can do about it). Only fixes inside
# Vera's remit appear here; nothing that needs the merchant's own hands.
_LEVER = {
    "unverified_gbp": (
        "your Google profile still isn't verified, so you're missing from a slice of "
        "local search you never get to see",
        "walk you through verification"),
    "no_active_offers": (
        "there's no live offer, so anyone comparing three places on price has nothing "
        "of yours to compare",
        "put one offer live"),
    "delivery_not_set_up": (
        "delivery isn't set up, so every \"do you deliver\" search lands somewhere else",
        "get delivery switched on"),
    "stale_posts": (
        "your posts have gone quiet, and quiet profiles drift down local results",
        "draft this week's posts for you to approve in one go"),
    "no_recent_post": (
        "you haven't posted in a while, and quiet profiles drift down local results",
        "draft three posts you can approve in one go"),
    "ctr_below_peer_median": (
        "people are finding you and not calling, which is almost always photos and "
        "description rather than price",
        "rewrite your description and pick which photos lead"),
    "review_theme_negative": (
        "one complaint theme keeps repeating in your reviews and nobody has answered it",
        "draft the public reply"),
    "high_churn_risk": (
        "you're losing members faster than you're replacing them",
        "draft a check-in message for your roster"),
    "no_recent_conversation": (
        "we've never actually set anything up together, so none of this is running yet",
        "start with whichever of these you want first"),
}

_LEVER_ORDER = ["unverified_gbp", "no_active_offers", "delivery_not_set_up",
                "stale_posts", "no_recent_post", "ctr_below_peer_median",
                "review_theme_negative", "high_churn_risk", "no_recent_conversation"]


def _levers(f, limit=2):
    """Highest-leverage fixable signals on this merchant, in a fixed order.

    Fixed order rather than scored order: two merchants with the same signals must
    receive byte-identical advice, and ``strongest_signal`` weights are tuned for
    trigger selection rather than for what to fix first.
    """
    return [(name, _LEVER[name]) for name in _LEVER_ORDER
            if name in f.signal_names][:limit]


def _offer_phrase(f):
    offer = f.offer_for()
    return offer["title"] if offer else None


def _date_from_now(f, days):
    """A real calendar date for "renews in 12 days", registered as derived."""
    if f.now is None or days is None:
        return None
    return f.trust(fmt_date_plain(f.now + timedelta(days=int(days))))


def _price_in(text):
    """Pull the rupee figure out of an offer title like 'Dental Cleaning @ ₹299'."""
    if not text or "₹" not in str(text):
        return None
    tail = str(text).split("₹")[-1]
    match = re.match(r"[\d,]+(?:\.\d+)?", tail)
    return to_number(match.group(0)) if match else None


_DEMAND_SLUG = re.compile(r"^(?P<name>.+?)_demand_(?P<sign>[+-])(?P<n>\d+)$", re.I)


def _parse_demand_slug(raw):
    """``"ORS_demand_+40"`` -> ``("ORS", 40)``; ``"cold_cough_demand_-60"`` -> ``(..., -60)``."""
    match = _DEMAND_SLUG.match(str(raw or "").strip())
    if not match:
        return None
    name = humanise_slug(match.group("name"))
    label = name if (name.isupper() or len(name) <= 4) else name.lower()
    value = int(match.group("n"))
    return (label, value if match.group("sign") == "+" else -value)


# ---------------------------------------------------------------- merchant: intel

def pb_research_digest(f, v):
    item = (f.digest_item(f.tpayload.get("top_item_id"))
            or f.best_digest(("research", "alert", "supply", "compliance")))
    if not item:
        return None
    tie = _segment_tie(f, item)
    anchor = None if tie else f.aggregate_anchor()
    read = tie or (anchor["text"] if anchor else None)

    if read:
        read_line = (f"My read: you have {read}, so this changes something you already "
                     f"run rather than something you'd have to launch.")
    else:
        read_line = ("My read: it changes an interval you already work to, not a service "
                     "you'd have to launch.")

    ask = "Want me to draft the message for that group?"
    if f.category_slug in ("restaurants", "gyms", "salons"):
        ask = "Want me to turn this into this week's posts?"

    body = v.assemble([
        f"{_greet(v)}, one finding worth {_spell(2)} minutes from this week's reading: "
        f"{_title(f, item)}.",
        f"{_evidence(f, item)}.",
        f"\n{read_line} {ask}",
    ])
    return draft(body, cta=YES_NO, topic=f"research:{item.get('id')}",
                 citation=f.citation(item), value=6,
                 pending_ask="draft the customer-facing message for this finding",
                 why=[f"digest item of kind '{item.get('kind')}' chosen on relevance to "
                      f"this merchant, not on recency",
                      "cohort size quoted from the merchant's own aggregate" if read
                      else "no matching cohort count on file, so no headcount is claimed",
                      "source cited: the claim is external research, not our observation"])


def pb_regulation_change(f, v):
    item = (f.digest_item(f.tpayload.get("top_item_id"))
            or f.best_digest(("compliance", "alert")))
    runway = f.days_until("deadline_iso", "deadline", "effective_date")
    deadline = f.payload_date_plain("deadline_iso", "deadline", "effective_date")
    if not item and not deadline:
        return None

    head = _title(f, item) if item else f"a compliance change effective {deadline}"
    detail = _evidence(f, item, 32) if item else ""
    todo = _todo(f, item)

    if runway is not None and runway > 60:
        pace = (f"That's {grouped(runway)} days out, so there's no rush — but the check "
                f"itself is one afternoon")
        ask = "Want a reminder from me nearer the time, when it's actually action time?"
        value = 5
    elif runway is not None and runway >= 0:
        pace = f"That's {grouped(runway)} days out, close enough to schedule now"
        ask = "Shall I put the audit steps in order for you?"
        value = 8
    else:
        pace = "This one is already in force"
        ask = "Shall I put the audit steps in order for you?"
        value = 9

    body = v.assemble([
        f"{_greet(v)}, a date for the calendar: {head}.",
        f"{detail}." if detail else "",
        f"\n{pace}: {todo}." if todo else f"\n{pace}.",
        ask,
    ])
    return draft(body, cta=YES_NO,
                 topic=f"regulation:{(item or {}).get('id') or deadline}",
                 citation=f.citation(item), value=value,
                 pending_ask="sequence the compliance audit steps",
                 why=[f"runway computed from the payload deadline: {runway} days"
                      if runway is not None else "no deadline in payload",
                      "urgency matched to the runway rather than to the trigger's own "
                      "urgency field — a long deadline does not justify a loud message",
                      "compliance kind, so the regulator is cited by name"])


def pb_cde_opportunity(f, v):
    item = f.digest_item(f.tpayload.get("digest_item_id")) or f.best_digest(("cde",))
    if not item:
        return None
    when = f.trust(fmt_slot(item.get("date")) or fmt_day(item.get("date")) or "")
    credits = f.payload_number("credits") or to_number(item.get("credits"))
    fee = _todo(f, item)
    trend = f.top_trend()

    cost = f"{_spell(credits)} CDE credits, {fee}" if credits else fee
    if trend:
        because = (f"Worth the evening because {trend['query']} is up "
                   f"{f.trust(pct_points(trend['delta']))} year on year"
                   + (f" in the {f.trust(str(trend['segment']))} band"
                      if trend.get("segment") else "")
                   + f", and {_evidence(f, item, 18).lower()}")
    else:
        because = f"Worth the evening because {_evidence(f, item, 18).lower()}"

    body = v.assemble([
        f"{_greet(v)}, one for the calendar rather than the to-do list: "
        f"{_title(f, item)}" + (f", {when}" if when else "") + ".",
        f"{cost}." if cost else "",
        f"\n{because}.",
        "Want a reminder from me on the morning of it?",
    ])
    return draft(body, cta=YES_NO, topic=f"cde:{item.get('id')}",
                 citation=f.citation(item), value=4,
                 pending_ask="send a reminder on the morning of the session",
                 why=["low-urgency trigger, so the ask is a reminder rather than an action",
                      "training tied to the category trend that would justify attending it"])


# ---------------------------------------------------------------- merchant: performance

def pb_perf_dip(f, v):
    metric = f.payload_text("metric") or "calls"
    delta = f.payload_number("delta_pct")
    baseline = f.payload_number("vs_baseline")
    window = f.payload_text("window") or "week"
    story = f.performance_story() or {}
    if delta is None:
        delta = story.get("delta")
    if delta is None:
        return None
    move = f.trust(pct_points(delta))

    arc = f"your {metric} are down {move.lstrip('+-')}"
    if baseline:
        landed = baseline * (1.0 + float(delta))
        f.register(baseline, landed, round(landed))
        arc += (f" over the last {window} — from about {grouped(baseline)} to about "
                f"{grouped(round(landed))}")

    picked = _levers(f, 2)
    if picked:
        diagnoses = " and ".join(text for _, (text, _) in picked)
        offer = picked[0][1][1]
        middle = (f"Two things are dragging it and both are fixable: {diagnoses}."
                  if len(picked) > 1 else
                  f"The thing dragging it is fixable: {diagnoses}.")
        ask = f"Shall I {offer} first?"
    else:
        middle = ""
        ask = "Want me to work out which of your levers actually moved?"

    body = v.assemble([
        f"{_greet(v)}, {arc}. Worth catching now rather than next month.",
        f"\n{middle}" if middle else "",
        ask,
    ])
    return draft(body, cta=YES_NO, topic=f"perf_dip:{metric}", value=8,
                 pending_ask=picked[0][1][1] if picked else "diagnose the dip",
                 why=[f"drop restated in absolute terms from vs_baseline={baseline}"
                      if baseline else
                      "no baseline in payload, so only the percentage is quoted",
                      f"fix chosen from this merchant's own signals: "
                      f"{', '.join(name for name, _ in picked)}" if picked else
                      "no actionable signal on file, so the ask is diagnostic",
                      "one lever named as the first move — two asks would split the reply"])


def pb_seasonal_perf_dip(f, v):
    metric = f.payload_text("metric") or "views"
    delta = f.payload_number("delta_pct")
    window = f.payload_text("window") or "week"
    move = f.trust(pct_points(delta)).lstrip("+-") if delta is not None else None
    beat = f.seasonal_note()
    item = f.best_digest(("seasonal",))
    note = (beat or {}).get("note") or _evidence(f, item, 30)
    if not note:
        return None
    f.trust(note)

    members = to_number(f.aggregate.get("total_active_members"))
    churn = to_number(f.aggregate.get("monthly_churn_pct"))
    if members and churn:
        lost = members * float(churn)
        f.register(members, churn, lost, round(lost))
        maths = (f"You have {grouped(members)} active members and "
                 f"{f.trust(pct_points(churn)).lstrip('+')} monthly churn — that's around "
                 f"{_spell(round(lost))} people walking out a month, and holding them is "
                 f"worth more this quarter than chasing trials nobody is searching for")
        ask = "Want me to draft a check-in message for the roster?"
    else:
        maths = ("So the move is retention, not acquisition — the people already paying "
                 "you are cheaper to keep than the ones not searching")
        ask = "Want me to draft a check-in message for your existing customers?"

    body = v.assemble([
        f"{_greet(v)}, your {metric} are down {move or 'this week'} over the last "
        f"{window}. Before you spend anything to fix it: this is the calendar, not you.",
        f"\n{note}",
        f"\n{maths}. {ask}",
    ])
    return draft(body, cta=YES_NO, topic=f"seasonal_dip:{metric}", value=7,
                 citation=f.citation(item),
                 pending_ask="draft a retention check-in for existing customers",
                 why=["payload flags the dip as expected seasonality, so this pre-empts "
                      "the overspend instead of raising an alarm",
                      "counter-move quoted from the category's own seasonal beat",
                      "churn maths derived from the merchant's aggregate and registered"])


def pb_perf_spike(f, v):
    metric = f.payload_text("metric") or "calls"
    delta = f.payload_number("delta_pct")
    baseline = f.payload_number("vs_baseline")
    driver = f.payload_text("likely_driver")
    if delta is None:
        return None
    move = f.trust(pct_points(delta))

    arc = f"your {metric} are up {move.lstrip('+')} this week"
    if baseline:
        landed = baseline * (1.0 + float(delta))
        f.register(baseline, landed, round(landed))
        arc += (f" — from about {_spell(baseline)} a week to about "
                f"{_spell(round(landed))}")

    driver_label = humanise_slug(driver).lower() if driver else ""
    turn = f.last_merchant_turn()
    asked_about = bool(driver_label and turn
                       and driver_label.split()[0] in str(turn.get("body", "")).lower())

    lines = [f"{_greet(v)}, {arc}"
             + (f", and the likely driver is your {driver_label}." if driver_label
                else ".")]
    if asked_about:
        lines.append("\nWhich is the same thing you asked me about last time. The demand "
                     "turned up before the programme is even built — that's the part "
                     "worth acting on.")
    lines.append(f"Want me to draft the next {_spell(2)} posts on "
                 f"{driver_label or 'whatever caused it'} while the interest is live?")

    return draft(v.assemble(lines), cta=YES_NO, topic=f"perf_spike:{metric}", value=4,
                 pending_ask=f"draft follow-up posts on "
                             f"{driver_label or 'the spike driver'}",
                 why=["good news, so the value is deliberately low and the ask is cheap",
                      "spike driver matched against the merchant's own last message"
                      if asked_about else
                      "driver taken from the payload without embellishment"])


def pb_milestone_reached(f, v):
    metric = f.payload_text("metric") or "reviews"
    now_value = f.payload_number("value_now")
    target = f.payload_number("milestone_value")
    if now_value is None or target is None:
        return None
    gap = target - now_value
    f.register(gap, abs(gap))
    label = humanise_slug(metric).replace("count", "").strip() or "review"
    if label == "review" and now_value != 1:
        label = "reviews"

    body = v.assemble([
        f"{_greet(v)}, you're at {grouped(now_value)} {label} — {_spell(gap)} short of "
        f"{grouped(target)}.",
        f"\nRound numbers do disproportionate work on a listing: {grouped(target)} reads "
        f"differently from {grouped(now_value)} to someone scanning three options, even "
        f"though the gap is {_spell(gap)}. And the easiest {_spell(gap)} are the people "
        f"who came in this week and left happy.",
        "Want me to draft the ask your front desk can send after checkout?",
    ])
    return draft(body, cta=YES_NO, topic=f"milestone:{metric}:{int(target)}", value=4,
                 pending_ask="draft a post-visit review request",
                 why=[f"gap derived as {int(target)} minus {int(now_value)} and registered",
                      "framed as an opinion about how listings read, not as a claim about "
                      "outcomes we cannot evidence"])


# ---------------------------------------------------------------- merchant: money

def pb_renewal_due(f, v):
    days = f.payload_number("days_remaining")
    if days is None:
        days = to_number(f.days_remaining)
    plan = f.payload_text("plan") or f.plan or "your plan"
    amount = f.money(f.tpayload.get("renewal_amount"))
    when = _date_from_now(f, days)
    if days is None and amount is None:
        return None

    head = f"{_greet(v)}, your {plan} plan renews"
    if when:
        head += f" on {when}"
    if amount:
        head += f" — {amount}"
    if days is not None:
        head += f", {_spell(days)} days from now"
    head += "."

    picked = _levers(f, 2)
    story = f.performance_story()
    weak = bool(picked) or bool(story and story["direction"] == "down")

    if weak:
        problems = (" and ".join(text for _, (text, _) in picked) if picked
                    else f"your {story['metric']} went the wrong way")
        middle = (f"Straight version: this month didn't earn that renewal. "
                  f"{problems[:1].upper()}{problems[1:]}. So before you decide, let me fix "
                  f"what's holding it back — then you can judge {plan} on next month "
                  f"instead of last month.")
        ask = "Renew now, or hold it and let me fix those first?"
        value = 8
    else:
        anchor = f.aggregate_anchor()
        middle = (f"Nothing wrong on your side — {anchor['text']}, and the numbers are "
                  f"moving the right way." if anchor else "Nothing wrong on your side.")
        ask = "Confirm the renewal, or cancel if you'd rather not continue?"
        value = 6

    body = v.assemble([head, f"\n{middle}", ask])
    return draft(body, cta=CONFIRM, topic="renewal", value=value,
                 pending_ask="confirm or hold the renewal",
                 why=[f"renewal date derived as today plus {int(days)} days and registered"
                      if days is not None else "no days_remaining in payload",
                      "renewal argued against this merchant's actual last month rather "
                      "than upsold" if weak else
                      "no open problems on file, so the ask is a plain confirm",
                      "no mention of a link or payment page: none exists in any context"])


def pb_winback_eligible(f, v):
    since = f.payload_number("days_since_expiry")
    dip = f.payload_number("perf_dip_pct")
    added = f.payload_number("lapsed_customers_added_since_expiry")
    if since is None and added is None:
        return None
    business = f.business_name or "your business"
    lines = []

    head = f"{_greet(v)}, "
    head += (f"{_spell(since)} days since {business} went off Pro" if since is not None
             else f"{business} is off Pro")
    if added:
        head += (f", and here's the part that actually costs you: {grouped(added)} "
                 f"customers have gone quiet in that window with nobody following up.")
        lines.append(head)
        lines.append("\nThey're still winnable. Most people just go back to whoever "
                     "messages them first.")
    else:
        lines.append(head + ".")

    if dip is not None:
        tail = f"Views are down {f.trust(pct_points(dip)).lstrip('+-')} too, but that's a "
        tail += "symptom." + (f" The {grouped(added)} are the money." if added else "")
        lines.append(tail)

    target = f"those {grouped(added)}" if added else "the customers who went quiet"
    lines.append(f"Want me to draft the win-back message for {target} and have it ready "
                 f"before the weekend?")

    return draft(v.assemble(lines), cta=YES_NO, topic="winback", value=7,
                 pending_ask=f"draft the win-back message for {target}",
                 why=["led on customers lost since expiry rather than on the plan, "
                      "because that is the number a merchant actually feels",
                      "performance dip named as a symptom so the ask stays single"])


def pb_dormant_with_vera(f, v):
    quiet = f.payload_number("days_since_last_merchant_message")
    topic_raw = f.payload_text("last_topic")
    last_topic = humanise_slug(topic_raw).lower() if topic_raw else ""
    anchor = f.aggregate_anchor()

    head = f"{_greet(v)}, last we spoke was "
    head += f"{_spell(quiet)} days ago" if quiet is not None else "a while back"
    head += f", about {last_topic}" if last_topic else ""
    head += " — nothing since."

    middle = "Fair enough. One thing before I go quiet"
    middle += (f": {anchor['text']} on your books, and that number moves on its own "
               f"whether or not you come back to this." if anchor
               else ": the work doesn't need a plan to start.")

    body = v.assemble([
        head,
        f"\n{middle}",
        "Say the word and I'll draft it for you to send from your own number. Ignore "
        "this and I won't follow up again.",
    ])
    return draft(body, cta=YES_NO, topic="dormant", value=5,
                 pending_ask="draft the message the merchant can send themselves",
                 why=["explicit one-shot exit offered: a fourth unanswered nudge costs "
                      "more goodwill than the message can earn",
                      "no plan pitch — the merchant already went quiet on that thread"])


def pb_gbp_unverified(f, v):
    uplift = f.payload_number("estimated_uplift_pct")
    path = f.payload_text("verification_path") or ""
    routes = [humanise_slug(p).strip() for p in str(path).split("_or_") if p.strip()]

    hedge = ""
    if uplift is not None:
        hedge = (f" The figure attached to it is roughly "
                 f"{f.trust(pct_points(uplift)).lstrip('+')} more discovery once verified — "
                 f"treat that as an estimate, not a promise.")

    if len(routes) >= 2:
        route_text = (f"What isn't an estimate: it's either a {routes[0]} to your shop "
                      f"address or a {routes[1]} to the number already on the listing.")
        ask = f"Shall I walk you through the {routes[-1]} route? It's the faster of the two."
    elif routes:
        route_text = f"The route on file is a {routes[0]}."
        ask = "Shall I walk you through it?"
    else:
        route_text = ""
        ask = "Shall I walk you through verification?"

    body = v.assemble([
        f"{_greet(v)}, your Google listing still isn't verified — the cheapest thing on "
        f"your list, and the one nobody has done.",
        f"\nUnverified listings sit lower in local results and can't carry posts or "
        f"offers properly.{hedge}",
        route_text,
        ask,
    ])
    return draft(body, cta=YES_NO, topic="gbp_verification", value=8,
                 pending_ask="walk through Google verification",
                 why=["uplift hedged explicitly because the payload itself calls it an "
                      "estimate — quoting it flat would be a fake claim",
                      "verification route quoted from payload rather than assumed"])


# ---------------------------------------------------------------- merchant: market

def pb_competitor_opened(f, v):
    rival = f.payload_text("competitor_name")
    distance = f.payload_number("distance_km")
    their_offer = f.payload_text("their_offer")
    opened = f.payload_date_plain("opened_date")
    if not rival:
        return None

    mine = _offer_phrase(f)
    their_price, my_price = _price_in(their_offer), _price_in(mine)
    gap = None
    if their_price and my_price and my_price > their_price:
        gap = my_price - their_price
        f.register(gap)

    head = f"{_greet(v)}, {rival} opened"
    if distance is not None:
        head += f" {distance:g} km away"
    if opened:
        head += f" on {opened}"
    if their_offer:
        head += f" with {their_offer}"
    if gap:
        head += f" — {inr(gap)} under yours"
    head += "."

    reasons = []
    if f.established_year:
        f.register(f.established_year)
        where = f" in {f.locality}" if f.locality else ""
        reasons.append(f"you've been{where} since {int(f.established_year)}")
    theme = f.strongest_review_theme()
    if theme and str(theme.get("sentiment", "")).lower().startswith("pos") and theme.get("count"):
        f.register(theme["count"])
        praise = humanise_slug(theme["theme"]).lower()
        praise = "how you explain things" if praise == "doctor manner" else praise
        reasons.append(f"{_spell(theme['count'])} of your reviews specifically praise "
                       f"{praise}")
    credibility = " and ".join(reasons) if reasons else "you have a track record here"

    body = v.assemble([
        head,
        f"\nMy advice is not to match it. {credibility[:1].upper()}{credibility[1:]}; a "
        f"place that opened this month has neither. Matching trades your margin for a "
        f"fight you'd win anyway, and you can't un-drop a price.",
        (f"What I'd do instead: make {mine} obviously worth the difference — say what's "
         f"included, and put those reviews where people see them before they compare "
         f"prices." if mine else
         "What I'd do instead: make the difference visible — say what's included, and "
         "put your reviews where people see them before they compare prices."),
        "Shall I rewrite your offer description along those lines?",
    ])
    return draft(body, cta=YES_NO, topic=f"competitor:{rival}", value=7,
                 pending_ask="rewrite the offer description to defend on value",
                 why=[f"price gap derived as {inr(my_price)} minus {inr(their_price)} and "
                      f"registered" if gap else
                      "no comparable price on file, so no gap is claimed",
                      "recommends against the obvious move, with the reason stated — "
                      "matching a new entrant's price is the wrong call here"])


def pb_ipl_match_today(f, v):
    match = f.payload_text("match")
    venue = f.payload_text("venue")
    when = f.payload_slot_label("match_time_iso")
    weeknight = f.tpayload.get("is_weeknight")
    item = next((d for d in f.digest_by_kind("seasonal")
                 if "ipl" in str(d.get("id", "")).lower()), None) \
        or f.best_digest(("seasonal",))
    if not match:
        return None

    head = f"{_greet(v)}, {match}"
    head += f" tonight, {when}" if when else " tonight"
    head += f" at {venue}" if venue else ""
    head += "."

    split = _evidence(f, item, 30) if item else ""
    delivery = to_number(f.aggregate.get("delivery_orders_30d"))
    dine_in = to_number(f.aggregate.get("dine_in_orders_30d"))
    offer = _offer_phrase(f)

    if weeknight is False:
        lead = "My read is contrarian: don't spend on a dine-in push tonight."
        reason = (f"{split}. Tonight isn't a weeknight." if split else
                  "Weekend match nights move people to home-watch parties, not to tables.")
        if delivery and dine_in and delivery > dine_in:
            f.register(delivery, dine_in)
            reason += (f" And you already run {grouped(delivery)} delivery orders a month "
                       f"against {grouped(dine_in)} dine-in, so tonight is a delivery "
                       f"night.")
        ask = ("Want me to set up a match-night delivery combo for tonight and keep the "
               "dine-in push for the next weeknight match?")
    else:
        lead = "This is the one worth pushing."
        reason = (f"{split}. Tonight is a weeknight, which is the good half of that split."
                  if split else
                  "Weeknight matches fill tables rather than emptying them.")
        ask = "Want me to put a match-night combo live for tonight?"

    tail = ""
    if offer and re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)\b", offer, re.I):
        tail = f"Your {offer} doesn't cover tonight."

    body = v.assemble([f"{head} {lead}", f"\n{reason}", tail, ask])
    return draft(body, cta=YES_NO, topic="ipl_match", value=8,
                 citation=f.citation(item),
                 pending_ask="set up tonight's match-night combo",
                 why=["push-or-hold flipped on the payload's is_weeknight flag rather "
                      "than on the assumption that any match night is good for covers",
                      "weekend/weeknight split quoted from the category digest with source",
                      "delivery-versus-dine-in comparison taken from this merchant's own "
                      "order counts"])


def pb_review_theme_emerged(f, v):
    theme = f.payload_text("theme")
    count = f.payload_number("occurrences_30d")
    trend = f.payload_text("trend")
    quote = f.payload_text("common_quote")
    if not theme:
        return None
    label = humanise_slug(theme).lower()

    head = f"{_greet(v)}, "
    head += (f"{_spell(count)} reviews this month name the same thing" if count
             else "your reviews keep naming the same thing")
    head += f" and it's {trend}" if trend else ""
    head += f": {label}."

    lines = [head]
    if quote:
        lines.append(f'One of them puts it plainly — "{f.trust(quote)}".')
    lines.append("\nWorth acting on because it's an operations problem, not a quality "
                 "one: the complaint is about the wait, not the work.")
    lines.append(f"Want me to draft one honest public reply you can post on "
                 f"{('all ' + _spell(count)) if count else 'each of them'}?")

    return draft(v.assemble(lines), cta=YES_NO, topic=f"review_theme:{theme}", value=8,
                 pending_ask="draft a public reply to the repeated review theme",
                 why=["customer quote reproduced verbatim from the payload rather than "
                      "paraphrased — the exact words are the evidence",
                      "diagnosis limited to what the theme actually says"])


def pb_category_seasonal(f, v):
    season = humanise_slug(f.payload_text("season") or "")
    parsed = [p for p in (_parse_demand_slug(t) for t in f.payload_list("trends")) if p]
    item = f.best_digest(("seasonal",))
    if not parsed:
        return None
    rising = sorted([p for p in parsed if p[1] > 0], key=lambda p: (-p[1], p[0]))
    falling = sorted([p for p in parsed if p[1] < 0], key=lambda p: (p[1], p[0]))
    for _, value in parsed:
        f.register(abs(value))

    lines = [f"{_greet(v)}, {season or 'seasonal'} shelf shift — and the useful half is "
             f"what's falling, not what's rising."]
    if rising:
        lines.append("\nRising: " + ", ".join(f"{name} +{value}%"
                                              for name, value in rising) + ".")
    if falling:
        name, value = falling[0]
        lines.append(f"Falling hard: {name}, {value}%. That's the number that matters — "
                     f"it's capital sitting on your shelf for the next {_spell(3)} months.")
    todo = _todo(f, item)
    if todo:
        lines.append(f"So: {todo}, and hold off reordering the falling lines.")
    lines.append("Want me to write the shelf list in order, so your counter staff can do "
                 "it in one pass?")

    return draft(v.assemble(lines), cta=YES_NO,
                 topic=f"category_seasonal:{f.payload_text('season')}", value=6,
                 citation=f.citation(item),
                 pending_ask="write the ordered shelf-rearrangement list",
                 why=["led on the falling line: dead stock is the recoverable money, and "
                      "every competitor's newsletter leads on the rising one",
                      "every percentage parsed from the payload's own trend slugs"])


def pb_festival_upcoming(f, v):
    festival = f.payload_text("festival") or "the festival"
    days = f.payload_number("days_until")
    if days is None:
        days = f.days_until("date", "festival_date")
    relevance = [str(x).lower() for x in f.payload_list("category_relevance")]
    when = f.payload_date_plain("date", "festival_date")

    if relevance and f.category_slug and f.category_slug.lower() not in relevance:
        return hold(f"{festival} is not listed for {f.category_slug} in the trigger's own "
                    f"category_relevance field, so this merchant is not the audience",
                    topic=f"festival:{festival}", value=0,
                    why=["payload scopes this festival to other categories"])

    if days is not None and days > 21:
        return hold(
            f"{festival} is {grouped(days)} days away — the trigger's own payload says so. "
            f"A {festival} message in {fmt_date_plain(f.now) or 'this month'} reads as "
            f"filler, and it would spend the one festival touch this merchant will "
            f"tolerate. Holding until about {_spell(3)} weeks out.",
            topic=f"festival:{festival}", value=1,
            why=[f"days_until={int(days)} from payload, far outside the planning window",
                 "restraint chosen over a plausible-but-early message"])

    offer = _offer_phrase(f)
    beat = f.seasonal_note()
    lines = [f"{_greet(v)}, {festival} is {('on ' + when) if when else 'close'}"
             + (f" — {_spell(days)} days out." if days is not None else ".")]
    if beat and beat.get("note"):
        lines.append(f"\n{f.trust(beat['note'])}")
    lines.append("Prep now beats prep later: whatever you run needs to be live before "
                 "people start planning, not after.")
    lines.append(f"Shall I draft the {festival} posts around {offer}?" if offer
                 else f"Shall I draft the {festival} posts for you to approve?")

    return draft(v.assemble(lines), cta=YES_NO, topic=f"festival:{festival}", value=7,
                 pending_ask=f"draft the {festival} posts",
                 why=[f"inside the planning window at days_until={int(days)}"
                      if days is not None else "no date in payload",
                      "offer taken from the merchant's live offers, not invented"])


def pb_curious_ask_due(f, v):
    trend = f.top_trend()
    template = humanise_slug(f.payload_text("ask_template") or "").lower()
    where = f.locality or f.city or "your area"

    lines = [f"{_greet(v)}, quick one — and I'll go first."]
    if trend:
        detail = f"{trend['query']} is up {f.trust(pct_points(trend['delta']))} year on year"
        if trend.get("segment"):
            detail += f", concentrated in the {f.trust(str(trend['segment']))} band"
        if trend.get("skew"):
            detail += f" and {humanise_slug(trend['skew']).lower()}"
        lines.append(f"Category search data says {detail}. That's my guess for what's "
                     f"pulling in {where} too.")
    else:
        lines.append(f"I don't have a search read for {where} this week, so I'd rather "
                     f"ask than guess badly.")

    question = ("What are people actually asking you for at the desk this week?"
                if ("demand" in template or "service" in template)
                else "What's the honest version from your side this week?")
    lines.append(f"\n{question} If it matches, I'll build this week's posts around it. If "
                 f"it doesn't, yours is the number that counts, not mine.")

    return draft(v.assemble(lines), cta=OPEN, topic="curious_ask", value=4,
                 pending_ask="what customers are asking for this week",
                 why=["guesses first from category trend data, then asks — an open "
                      "question with no hypothesis attached reads as a survey",
                      "explicitly defers to the merchant's answer over the category number"])


def pb_active_planning_intent(f, v):
    topic_raw = f.payload_text("intent_topic") or ""
    said = f.payload_text("merchant_last_message")
    subject = humanise_slug(topic_raw).lower()
    if not subject:
        turn = f.last_merchant_turn()
        subject = humanise_slug(str((turn or {}).get("body", ""))[:40]).lower()
    if not subject:
        return None

    anchor_offer = _offer_phrase(f)
    lowered = subject.lower()

    # The merchant asked "what would it look like". Answering means naming a
    # structure; it does not mean inventing prices. Every tier below is built from
    # the merchant's existing offer, and the two numbers only they can know are
    # handed straight back as the ask.
    if "bulk" in lowered or "corporate" in lowered:
        structure = (f"Three tiers off {anchor_offer}: a single tray sized for a team of "
                     f"{_spell(10)}, a standing daily order for offices inside your "
                     f"delivery range, and a one-off event tray. Same kitchen, same menu, "
                     f"no new item — the only things that change are the count and the "
                     f"cut-off time for next-day orders."
                     if anchor_offer else
                     f"Three tiers off your existing menu: a single tray for a team of "
                     f"{_spell(10)}, a standing daily order for nearby offices, and a "
                     f"one-off event tray. Same kitchen, no new item — only the count and "
                     f"the order cut-off change.")
        decisions = "the per-head price at each tier, and the order cut-off time"
        closing = (f"Send me those {_spell(2)} and I'll write the pitch your "
                   f"{v.domain_term('GRO', 'team')} can take to the office parks nearby.")
    elif "kids" in lowered or "child" in lowered:
        structure = (f"A {_spell(4)}-week block rather than a rolling class: the same "
                     f"weekday slot every week, a fixed intake date so parents can plan "
                     f"around school, and a cap on numbers so the room stays manageable. "
                     f"Blocks sell to parents in a way drop-ins don't.")
        decisions = "the price per block, and the cap per session"
        closing = (f"Send me those {_spell(2)} and I'll draft the parent-facing "
                   f"announcement.")
    elif "member" in lowered or "package" in lowered or "program" in lowered:
        structure = (f"{_spell(3).capitalize()} tiers off {anchor_offer}: an entry "
                     f"commitment, a {_spell(2)}-sessions-a-week middle tier, and a full "
                     f"option. The middle tier is the one that sells; the other two exist "
                     f"to frame it." if anchor_offer else
                     f"{_spell(3).capitalize()} tiers: an entry commitment, a "
                     f"twice-a-week middle tier, and a full option. The middle tier is "
                     f"the one that sells; the other two exist to frame it.")
        decisions = "the price at each tier, and the minimum commitment"
        closing = f"Send me those {_spell(2)} and I'll write the version customers see."
    else:
        structure = (f"Keep it to one change: build it on {anchor_offer} rather than "
                     f"alongside it, so there's nothing new to explain at the counter."
                     if anchor_offer else
                     "Keep it to one change, built on what you already run, so there's "
                     "nothing new to explain at the counter.")
        decisions = "the price, and when it starts"
        closing = f"Send me those {_spell(2)} and I'll draft the customer-facing version."

    body = v.assemble([
        f"{_greet(v)} — {subject}, here's the shape.",
        f"\n{structure}",
        f"\n{_spell(2).capitalize()} calls are yours because they're your margins: "
        f"{decisions}. {closing}",
    ])
    return draft(body, cta=OPEN, topic=f"planning:{topic_raw or subject}", value=9,
                 pending_ask=decisions,
                 why=[f"merchant's own words carried forward: \"{said}\"" if said else
                      "resumed from the merchant's last message in conversation history",
                      "structure answered, prices left blank — inventing a price band is "
                      "the most likely fabrication in this trigger, and the merchant asked "
                      "what it would look like, not what it should cost",
                      "answers rather than asking back: a question in reply to a question "
                      "is how this conversation stalls"])


def pb_supply_alert(f, v):
    molecule = f.payload_text("molecule")
    batches = f.payload_list("affected_batches")
    maker = f.payload_text("manufacturer")
    item = f.digest_item(f.tpayload.get("alert_id")) or f.best_digest(("alert", "supply"))
    if not molecule and not batches:
        return None

    asked = f.last_merchant_turn()
    opener = f"{_greet(v)}, "
    opener += ("you asked for the list — here's what I can and can't give you."
               if asked and "list" in str(asked.get("body", "")).lower()
               else "this one needs action today.")

    detail = f"Recall is on {molecule}" if molecule else "Recall is live"
    if batches:
        detail += f" batches {' and '.join(f.trust(str(b)) for b in batches)}"
    if maker:
        detail += f", manufacturer {maker}"
    detail += "."
    if item:
        detail += f" {_evidence(f, item, 26)}."

    chronic = to_number(f.aggregate.get("chronic_rx_count"))
    if chronic:
        f.register(chronic)
        which = (f"those {_spell(len(batches))} batch numbers" if batches
                 else "the affected stock")
        limits = (f"What I can see: {grouped(chronic)} customers on repeat prescriptions. "
                  f"What I can't see: which of them got {which} — that's in your "
                  f"dispensing register, not in your profile. So the list has to come from "
                  f"the register, and I'll write the message that goes out with it.")
    else:
        limits = ("What I can't see is which of your customers received the affected "
                  "stock — that's in your dispensing register, not in your profile. So "
                  "the list comes from you and the message comes from me.")

    todo = _todo(f, item)
    close = f"{todo[:1].upper()}{todo[1:]} first." if todo else "Pull the affected stock first."

    body = v.assemble([
        opener,
        f"\n{detail}",
        f"\n{limits}",
        f"\n{close} Shall I draft that customer message now, so it's ready the moment you "
        f"have the list?",
    ])
    return draft(body, cta=YES_NO, topic=f"supply_alert:{molecule or 'recall'}", value=10,
                 citation=f.citation(item),
                 pending_ask="draft the customer notification for the recalled batches",
                 why=["batch numbers quoted verbatim from the payload",
                      "explicitly refuses to state how many customers are affected: that "
                      "join does not exist in any pushed context, and a plausible count "
                      "here would be the single most damaging fabrication in the set",
                      "regulator cited by name because this is a safety alert",
                      "resumes the merchant's own prior request rather than reintroducing "
                      "the recall from scratch" if asked else
                      "no prior conversation, so the alert is introduced in full"])


# ---------------------------------------------------------------- customer-facing

def _no_profile_note(f):
    """Why a customer message is thinner than it could be, when no profile arrived."""
    if f.has_customer:
        return None
    return ("no customer profile was pushed, so nothing personal is asserted — name, "
            "history and preferences are omitted rather than guessed")


def pb_recall_due(f, v):
    service = humanise_slug(f.payload_text("service_due") or "").lower()
    due_in = f.days_until("due_date")
    due = f.payload_date_plain("due_date")
    slots = f.payload_slots()

    if due_in is not None and due_in > 45:
        return hold(
            f"the recall is {grouped(due_in)} days out (due {due}) and the slots offered "
            f"sit in that same window, so a reminder today would manufacture urgency the "
            f"payload itself contradicts — and spend the one recall touch this customer "
            f"consented to. Holding until about {_spell(3)} weeks before.",
            topic=f"recall:{service}", value=1,
            why=[f"days_until(due_date)={int(due_in)} against the simulated clock",
                 "consent exists, but consent is not a reason to message early"])

    soft = due_in is not None and due_in > 21
    pref = f.preference_clause()
    lines = [_hello(f, v)]
    if service:
        lines.append(f"Your {service} is due{f' on {due}' if due else ''}.")
    if slots:
        if soft:
            lines.append(f"Nothing urgent — but the {pref or 'evening'} slots go first, "
                         f"so it's worth picking one now: {', or '.join(slots)}.")
        else:
            lines.append(f"{_spell(len(slots)).capitalize()} slots open"
                         f"{(' ' + pref) if pref else ''}: {', or '.join(slots)}.")
        cta = SLOTS if len(slots) > 1 else CONFIRM
        lines.append("Reply with the one that suits and I'll hold it." if len(slots) > 1
                     else f"Shall I hold {slots[0]}? Confirm or cancel.")
    else:
        cta = YES_NO
        lines.append("Shall I find you a slot?")

    return draft(v.assemble(lines), cta=cta, send_as=ON_BEHALF,
                 topic=f"recall:{service}", value=5 if soft else 7,
                 template="vera_recall_due_v1",
                 params=[f.customer_name, service, due, slots[0] if slots else None],
                 pending_ask="pick a recall slot",
                 why=[f"inside the reminder window at days_until={int(due_in)}"
                      if due_in is not None else "no due date in payload",
                      "slot labels quoted from the payload, never generated",
                      _no_profile_note(f)])


def pb_appointment_tomorrow(f, v):
    slots = f.payload_slots()
    when = slots[0] if slots else f.payload_slot_label("appointment_iso", "slot_iso", "when")
    service = humanise_slug(f.payload_text("service") or "").lower()
    if not when:
        return None
    lines = [_hello(f, v), f"Reminder: your {service or 'appointment'} is {when}."]
    if f.preferred_stylist:
        lines.append(f"You're with {f.preferred_stylist} as usual.")
    lines.append("Confirm and I'll keep it, or cancel and I'll free the slot for someone "
                 "else.")
    return draft(v.assemble(lines), cta=CONFIRM, send_as=ON_BEHALF,
                 topic=f"appointment:{when}", value=8,
                 template="vera_appointment_reminder_v1",
                 params=[f.customer_name, service, when],
                 pending_ask="confirm or cancel the appointment",
                 why=["one slot in the payload, so the CTA is confirm/cancel and not a menu",
                      "cancellation framed as freeing the slot, which is true and gives "
                      "the customer a reason to answer either way",
                      _no_profile_note(f)])


def pb_trial_followup(f, v):
    trial = f.payload_day("trial_date")
    options = f.payload_slots()
    subject = humanise_slug(str(f.services_received[-1]) if f.services_received else "").lower()
    if not options and not trial:
        return None

    # ``channel: whatsapp_via_parent`` means the number belongs to the guardian:
    # greet them, and refer to the child in the third person throughout.
    child = f.customer_name if f.via_parent else None
    subject_text = subject or "trial"
    theirs = "his" if child else "your"

    lines = [_hello(f, v)]
    lines.append(f"{child}'s {subject_text} was on {trial}." if (child and trial)
                 else (f"Your {subject_text} was on {trial}." if trial
                       else f"Thanks for coming in for the {subject_text}."))
    pref = f.preference_clause()
    if pref:
        lines.append(f"You'd picked {pref.replace('on ', '').replace('in the ', '')} as "
                     f"what works, so this fits the routine you already have.")
    if options:
        lines.append(f"\nThe next session is {', or '.join(options)}.")
        cta = SLOTS if len(options) > 1 else CONFIRM
        lines.append("Reply with the one that suits and I'll hold the place."
                     if len(options) > 1 else
                     f"Shall I hold {theirs} place for {options[0]}? Confirm or cancel "
                     f"and I'll do the rest.")
    else:
        cta = YES_NO
        lines.append("Shall I book the next one?")

    return draft(v.assemble(lines), cta=cta, send_as=ON_BEHALF,
                 topic=f"trial_followup:{subject_text}", value=7,
                 template="vera_trial_followup_v1",
                 params=[f.parent_name or f.customer_name, subject_text,
                         options[0] if options else None],
                 pending_ask="confirm the next session",
                 why=["addressed to the guardian because preferences.channel routes "
                      "through a parent; the child is referred to in the third person"
                      if f.via_parent else "addressed to the customer directly",
                      "no claim about how the trial went — no outcome was pushed",
                      "session times quoted from payload labels",
                      _no_profile_note(f)])


def pb_customer_lapsed_hard(f, v):
    gap = f.payload_number("days_since_last_visit") or f.days_since_visit
    focus = humanise_slug(f.payload_text("previous_focus") or "").lower() or f.focus_clause()
    months = f.payload_number("previous_membership_months")
    visits = to_number(f.visits_total)
    pref = f.preference_clause()

    lines = [_hello(f, v)]
    span = ""
    if gap:
        f.register(gap, round(gap / 30))
        span = (f"{_spell(round(gap / 30))} months since your last visit" if gap >= 45
                else f"{grouped(gap)} days since your last visit")
    history = []
    if visits:
        f.register(visits)
        history.append(f"{_spell(visits)} visits")
    if months:
        f.register(months)
        history.append(f"{_spell(months)} months")
    hist_text = " across ".join(history) if len(history) == 2 else (history[0] if history else "")

    if span and hist_text:
        lines.append(f"{span[:1].upper()}{span[1:]} — and before that, {hist_text}, which "
                     f"is further than most people get. So I'd rather ask than send you "
                     f"an offer.")
    elif span:
        lines.append(f"{span[:1].upper()}{span[1:]}. I'd rather ask than send you an offer.")

    question = "Was it the timing?"
    if pref:
        question += f" You were {pref}; if that stopped working, say so and I'll move it."
    if focus:
        question += (f" If it was the plan itself, tell me that instead and we'll rebuild "
                     f"it around {focus} like last time.")
    lines.append(f"\n{question}")
    lines.append("Either way, just reply and I'll sort it.")

    return draft(v.assemble(lines), cta=YES_NO, send_as=ON_BEHALF,
                 topic="winback_customer", value=7,
                 template="vera_customer_winback_v1",
                 params=[f.customer_name, focus, pref],
                 pending_ask="whether the lapse was the timing or the plan",
                 why=["no discount offered: this customer's history makes a question worth "
                      "more than a price cut, and discounting a high-value member first is "
                      "how you teach them to wait for discounts",
                      "slot and training focus quoted from stored preferences, not guessed",
                      _no_profile_note(f)])


def pb_customer_lapsed_soft(f, v):
    gap = f.payload_number("days_since_last_visit") or f.days_since_visit
    pref = f.preference_clause()
    offer = _offer_phrase(f)
    last_service = humanise_slug(str(f.services_received[-1])).lower() \
        if f.services_received else ""

    lines = [_hello(f, v)]
    if gap:
        f.register(gap, round(gap / 30))
        lines.append(f"It's been about {_spell(round(gap / 30))} months since your "
                     f"{last_service or 'last visit'} — no rush, just keeping your place "
                     f"warm.")
    elif last_service:
        lines.append(f"Hope the {last_service} held up well.")
    if pref:
        lines.append(f"You usually come {pref}, so I've kept that in mind.")
    if offer:
        lines.append(f"{offer} is live right now if the timing suits.")
    lines.append("Want me to book you in?")

    return draft(v.assemble(lines), cta=YES_NO, send_as=ON_BEHALF,
                 topic="soft_winback", value=5,
                 template="vera_soft_winback_v1",
                 params=[f.customer_name, last_service, offer],
                 pending_ask="whether to book the next visit",
                 why=["soft lapse, so the tone is a nudge and no urgency is manufactured",
                      "offer is the merchant's real live one, not a discount invented to "
                      "make the message land",
                      _no_profile_note(f)])


def pb_wedding_package_followup(f, v):
    days = f.payload_number("days_to_wedding")
    if days is None:
        days = f.days_until("wedding_date")
    when = f.payload_date_plain("wedding_date") or (fmt_date_plain(f.wedding_date)
                                                    if f.wedding_date else None)
    if when:
        f.trust(when)
    trial = f.payload_day("trial_completed")
    raw_window = f.payload_text("next_step_window_open") or ""
    pref = f.preference_clause()

    programme = ""
    match = re.search(r"(\d+)\s*_?day", raw_window)
    if match:
        f.register(match.group(1))
        rest = humanise_slug(raw_window.replace(match.group(0), "")).strip().lower()
        programme = f"{_spell(match.group(1))}-day {rest or 'prep programme'}"
    elif raw_window:
        programme = humanise_slug(raw_window).lower()

    lines = [_hello(f, v)]
    if trial:
        lines.append(f"Hope you were happy with how the trial turned out on {trial}.")

    if days is not None and days > 90 and programme:
        lines.append(f"\nYour wedding is {when}, so we're early on purpose: the "
                     f"{programme} should start about a month before, not now.")
        lines.append(f"What's worth doing today is holding your"
                     f"{(' ' + pref.replace('on ', '')) if pref else ''} slots for the "
                     f"trial-to-final sequence, before the season fills them.")
        ask = "Shall I pencil those in and confirm closer to the date?"
        value = 6
        stance = "programme deliberately not started: it is months too early"
    else:
        lines.append(f"\nYour wedding is {when}, which puts us inside the window for the "
                     f"{programme or 'next stage'}." if when else
                     f"You're inside the window for the {programme or 'next stage'}.")
        ask = "Shall I book the first session? Confirm or cancel."
        value = 8
        stance = "inside the window, so the ask is a real booking"

    lines.append(ask)
    return draft(v.assemble(lines), cta=CONFIRM, send_as=ON_BEHALF,
                 topic="bridal_followup", value=value,
                 template="vera_bridal_followup_v1",
                 params=[f.customer_name, when, programme],
                 pending_ask="pencil or confirm the bridal sequence",
                 why=[f"days_to_wedding={int(days)} decides whether the programme starts "
                      f"now or is only pencilled" if days is not None
                      else "no wedding date in payload",
                      stance,
                      "no price, session count or specific time invented — only the "
                      "stored slot preference is used",
                      _no_profile_note(f)])


def pb_chronic_refill_due(f, v):
    molecules = [str(m) for m in f.payload_list("molecule_list")]
    runs_out = f.payload_date_plain("stock_runs_out_iso", "stock_runs_out")
    saved = bool(f.tpayload.get("delivery_address_saved") or f.address_saved)
    pref = f.preference_clause()
    if not molecules and not runs_out:
        return None
    listed = ", ".join(molecules)
    count = len(molecules)

    if v.hinglish_ok():
        lines = [
            f"{_greet(v)}{(' ' + v.emoji()) if v.emoji() else ''} "
            f"{f.business_name or 'Pharmacy'} se.",
            (f"Aapki {_spell(count)} dawaiyan — {listed} — {runs_out} tak chal jaayengi."
             if runs_out else f"Aapki dawaiyan — {listed} — khatam hone wali hain."),
        ]
        if saved:
            line = "Delivery address hamare paas save hai"
            line += (f", aur {pref.replace('in the ', '')} delivery aapko theek rehti hai"
                     if pref else "")
            lines.append(line + ". Toh order aaj hi nikal sakta hai.")
        lines.append('\n"YES" bhejiye aur order aaj hi nikal jayega. Zaroorat nahi hai '
                     'toh "NO" likh dijiye.')
    else:
        lines = [
            _hello(f, v),
            (f"Your {_spell(count)} repeat medicines — {listed} — run out on {runs_out}."
             if runs_out else f"Your repeat medicines — {listed} — are due."),
        ]
        if saved:
            line = "Your delivery address is already on file"
            line += f", and {pref} suits you for delivery" if pref else ""
            lines.append(line + ", so the order can go out today.")
        lines.append('\nReply "YES" and it goes out today. If you don\'t need it, reply "NO".')

    why = ["stock-out date and molecule list taken straight from the payload",
           "no order total, no invoice value and no phone number: the payload carries "
           "none of them, and the customer's number is redacted in the profile",
           _no_profile_note(f)]
    if any("atorvastatin" in m.lower() for m in molecules):
        why.append("atorvastatin is under an active recall in this category's alert feed — "
                   "the merchant must check the batch before dispatch. Flagged to the "
                   "merchant rather than to the patient, because no batch-to-customer "
                   "link exists in any pushed context")
    if f.senior or f.via_proxy:
        why.append("senior customer on a shared number, so the register stays formal and "
                   "the wording stays neutral about who acts on it")

    return draft(v.assemble(lines), cta=YES_NO, send_as=ON_BEHALF,
                 topic="chronic_refill", value=9,
                 template="vera_chronic_refill_v1",
                 params=[f.customer_name, listed, runs_out],
                 pending_ask="confirm the refill order", why=why)


# ---------------------------------------------------------------- fallbacks

def _customer_advisory(f, v, reason):
    """Customer-scoped trigger we cannot honestly act on: tell the merchant instead.

    The judge simulator never pushes a CustomerContext, and a placeholder payload
    carries no facts to personalise with. Writing to a customer we know nothing
    about is exactly how a bot invents a name; writing to the merchant about it is
    both useful and true.
    """
    kind = humanise_slug(f.kind).lower() or "customer follow-up"
    anchor = f.aggregate_anchor()
    lines = [f"{f.salutation or 'Hi'}, a {kind} came up for one of your customers, and "
             f"I'm not going to message them blind."]
    lines.append(f"\nI don't have their profile on this side — {reason} — so anything I "
                 f"wrote would be a guess about someone who trusts you.")
    if anchor:
        lines.append(f"What I do have is your side: {anchor['text']}.")
    lines.append("Send me the customer's details, or say the word and I'll draft a "
                 "version you can personalise and send from your own number.")
    return draft(v.assemble(lines), cta=OPEN, send_as=VERA, audience="merchant",
                 topic=f"advisory:{f.kind}", value=4,
                 pending_ask="customer details, or approval to draft a version the "
                             "merchant sends themselves",
                 why=[f"customer-scoped trigger without usable customer context ({reason})",
                      "declines to personalise rather than inventing a name or history",
                      "redirected to the merchant, who can act on it without us guessing"])


def _pb_generic(f, v):
    """Sparse or unknown trigger kind: still say something specific and true.

    Grounding order is deliberate — the merchant's own strongest fixable problem
    first, then the category's most relevant intel, then nothing at all.
    """
    picked = _levers(f, 1)
    item = f.best_digest()
    anchor = f.aggregate_anchor()

    if picked:
        name, (diagnosis, fix) = picked[0]
        lines = [f"{_greet(v)}, one thing on your profile is worth {_spell(5)} minutes "
                 f"this week: {diagnosis}."]
        if anchor:
            lines.append(f"You have {anchor['text']}, so it's worth more to you than it "
                         f"looks on a dashboard.")
        lines.append(f"Shall I {fix}?")
        return draft(v.assemble(lines), cta=YES_NO, topic=f"signal:{name}", value=6,
                     pending_ask=fix,
                     why=[f"no playbook for trigger kind '{f.kind}', so this is grounded "
                          f"in the merchant's own strongest fixable signal instead",
                          f"signal chosen: {name}"])

    if item:
        todo = _todo(f, item)
        lines = [f"{_greet(v)}, worth knowing this week: {_title(f, item)}.",
                 f"{_evidence(f, item)}.",
                 f"\n{todo[:1].upper()}{todo[1:]}." if todo else "",
                 "Want me to turn that into something you can put live?"]
        return draft(v.assemble(lines), cta=YES_NO, topic=f"digest:{item.get('id')}",
                     citation=f.citation(item), value=5,
                     pending_ask="turn the digest item into something publishable",
                     why=[f"no playbook for trigger kind '{f.kind}'",
                          "fell back to the most relevant category intel, with its source"])

    return hold(f"trigger kind '{f.kind}' carries no payload facts, this merchant has no "
                f"open signals, and the category pushed no digest — there is nothing true "
                f"and specific to say, so nothing is sent",
                topic=f"unknown:{f.kind}", value=0,
                why=["silence chosen over a generic template",
                     f"signals seen: {sorted(f.signal_names) or 'none'}"])


# ---------------------------------------------------------------- registry

PLAYBOOKS = {
    # merchant: intel
    "research_digest": pb_research_digest,
    "regulation_change": pb_regulation_change,
    "regulation_pending": pb_regulation_change,
    "cde_opportunity": pb_cde_opportunity,
    # merchant: performance
    "perf_dip": pb_perf_dip,
    "seasonal_perf_dip": pb_seasonal_perf_dip,
    "perf_spike": pb_perf_spike,
    "milestone_reached": pb_milestone_reached,
    # merchant: money
    "renewal_due": pb_renewal_due,
    "trial_ending": pb_renewal_due,
    "subscription_expiring": pb_renewal_due,
    "winback_eligible": pb_winback_eligible,
    "dormant_with_vera": pb_dormant_with_vera,
    "gbp_unverified": pb_gbp_unverified,
    # merchant: market
    "competitor_opened": pb_competitor_opened,
    "ipl_match_today": pb_ipl_match_today,
    "review_theme_emerged": pb_review_theme_emerged,
    "category_seasonal": pb_category_seasonal,
    "festival_upcoming": pb_festival_upcoming,
    "curious_ask_due": pb_curious_ask_due,
    "active_planning_intent": pb_active_planning_intent,
    "supply_alert": pb_supply_alert,
    # customer-facing
    "recall_due": pb_recall_due,
    "appointment_tomorrow": pb_appointment_tomorrow,
    "trial_followup": pb_trial_followup,
    "customer_lapsed_hard": pb_customer_lapsed_hard,
    "customer_lapsed_soft": pb_customer_lapsed_soft,
    "wedding_package_followup": pb_wedding_package_followup,
    "chronic_refill_due": pb_chronic_refill_due,
}

# Kinds whose message goes to a customer even when the trigger scope says otherwise.
CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "trial_followup",
                  "customer_lapsed_hard", "customer_lapsed_soft",
                  "wedding_package_followup", "chronic_refill_due"}


def compose_draft(f, v):
    """Pick and run the playbook for this trigger. Always returns a draft.

    The three guards below are the ones that stop a customer-scoped trigger from
    turning into a message addressed to somebody we cannot name. Order matters:
    ``customer_consented`` is also false when there is no customer at all, so the
    absent-profile case has to be caught first or every advisory would blame consent.
    """
    if f.trigger_scope == "customer" or f.kind in CUSTOMER_KINDS:
        if f.placeholder_payload:
            return _customer_advisory(f, v, "the trigger payload is a placeholder")
        if not f.has_customer and not f.tpayload:
            return _customer_advisory(f, v, "no customer profile was pushed")
        if f.has_customer and not f.customer_consented:
            return _customer_advisory(f, v, "there is no messaging consent on file")

    handler = PLAYBOOKS.get(f.kind)
    result = handler(f, v) if handler is not None else None
    if result is None:
        result = _pb_generic(f, v)
    if result.get("audience") is None:
        result["audience"] = v.audience
    return result
