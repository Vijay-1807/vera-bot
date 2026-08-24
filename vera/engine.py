"""Deterministic orchestration: composition, validation, ranking, and replies."""

from __future__ import annotations

import re
from datetime import timedelta

from . import config
from .facts import build_facts
from .playbooks import NONE, ON_BEHALF, OPEN, VERA, YES_NO, compose_draft
from .store import Store, resolve_now
from .util import normalise, normalise_tight, parse_iso, slug_token, stable_id
from .voice import Voice


QUESTION = re.compile(r"\?")
AUTO_REPLY = (
    "thank you for contacting", "thanks for contacting", "we will respond shortly",
    "will get back to you", "our team will respond", "business hours", "away message",
)
OPT_OUT = (
    "stop messaging", "do not message", "dont message", "unsubscribe", "opt out",
    "leave me alone", "not interested", "never contact",
)
WAIT_WORDS = ("later", "busy", "tomorrow", "next week", "call me", "not now")
ACCEPT = (
    "yes", "yeah", "yep", "sure", "go ahead", "do it", "lets do it", "let's do it",
    "send it", "please do", "confirm", "book it", "sounds good", "ok do",
)
REJECT = ("no thanks", "no thank", "decline", "cancel", "not needed")
OFF_TOPIC = ("gst", "tax filing", "income tax", "loan", "legal case", "accounting")


class Engine:
    def __init__(self, store=None):
        self.store = store or Store()

    def compose(self, trigger, now):
        facts = build_facts(self.store, trigger, now)
        if not facts.merchant_id or not facts.merchant:
            return None, "missing merchant context"
        if not facts.category:
            return None, "missing category context"
        voice = Voice(facts)
        draft = compose_draft(facts, voice)
        if draft.get("defer"):
            return None, draft.get("defer_reason") or "playbook deferred"

        body = voice.scrub(draft.get("body"))
        citation = voice.scrub(draft.get("citation")) if draft.get("citation") else ""
        if citation and normalise_tight(citation) not in normalise_tight(body):
            body = f"{body} — {citation}"

        problems = []
        stray = facts.unregistered_numbers(body)
        if stray:
            problems.append(f"unregistered numbers: {', '.join(stray)}")
        if voice.taboo_hits(body):
            problems.append("category taboo remained after scrubbing")
        if voice.jargon_hits(body):
            problems.append("internal identifier remained after scrubbing")
        if draft.get("cta") != NONE and len(QUESTION.findall(body)) > 1:
            problems.append("more than one question")
        if not body:
            problems.append("empty body")
        if problems:
            self.store.bump("validator_blocks")
            return None, "; ".join(problems)

        suppression_key = (facts.suppression_key or
                           f"{facts.kind}:{facts.merchant_id}:{facts.customer_id or 'merchant'}")
        conversation_id = self._conversation_id(facts, draft, suppression_key)
        why = list(draft.get("why") or [])
        why.extend(facts.provenance[:3])
        rationale = "; ".join(dict.fromkeys(str(item) for item in why if item))
        action = {
            "conversation_id": conversation_id,
            "merchant_id": facts.merchant_id,
            "customer_id": facts.customer_id,
            "send_as": draft.get("send_as") or (ON_BEHALF if facts.customer_id else VERA),
            "trigger_id": facts.trigger_id,
            "template_name": draft.get("template_name") or f"vera_{slug_token(facts.kind)}_v1",
            "template_params": draft.get("template_params") or [facts.salutation, draft.get("topic")],
            "body": body,
            "cta": draft.get("cta") or OPEN,
            "suppression_key": suppression_key,
            "rationale": rationale or "Selected the strongest grounded next action for this trigger.",
        }
        meta = {
            "value": int(draft.get("value") or 0),
            "urgency": facts.urgency_value,
            "topic": draft.get("topic") or facts.kind,
            "pending_ask": draft.get("pending_ask") or "take the proposed next step",
            "audience": draft.get("audience") or voice.audience,
        }
        return (action, meta), None

    def tick(self, payload):
        now = resolve_now(payload.get("now"))
        available = payload.get("available_triggers")
        trigger_ids = available if isinstance(available, list) else []
        candidates = []
        for position, trigger_id in enumerate(trigger_ids):
            trigger = self.store.trigger(trigger_id)
            if not isinstance(trigger, dict):
                continue
            result, reason = self.compose(trigger, now)
            if result is None:
                self.store.bump("holds")
                continue
            action, meta = result
            if not self._eligible(action, trigger, now):
                self.store.bump("holds")
                continue
            score = self._score(trigger, meta, now)
            if score < config.SCORE_FLOOR:
                continue
            candidates.append((-score, position, action, meta))

        actions = []
        touched_merchants = set()
        for _, _, action, meta in sorted(candidates, key=lambda row: (row[0], row[1])):
            merchant_id = action["merchant_id"]
            if merchant_id in touched_merchants:
                continue
            touched_merchants.add(merchant_id)
            self.store.record_send(action, now, meta["topic"])
            conversation = self.store.conversation(action["conversation_id"])
            conversation.topic = meta
            actions.append(action)
            if len(actions) >= config.MAX_ACTIONS_PER_TICK:
                break
        self.store.bump("ticks")
        return {"actions": actions}

    def reply(self, payload):
        now = resolve_now(payload.get("received_at"))
        conversation_id = payload.get("conversation_id")
        merchant_id = payload.get("merchant_id")
        customer_id = payload.get("customer_id")
        conversation = self.store.ensure_conversation(conversation_id, merchant_id, customer_id)
        merchant_id = conversation.merchant_id or merchant_id
        text = str(payload.get("message") or "").strip()
        role = payload.get("from_role") or ("customer" if customer_id else "merchant")
        conversation.add_inbound(role, text, payload.get("received_at"))
        self.store.bump("replies")
        clean = normalise(text)

        if conversation.state == "closed":
            return {"action": "end", "rationale": "Conversation is already closed."}
        if self._contains(clean, OPT_OUT):
            conversation.state, conversation.stage = "closed", "closed"
            conversation.close_reason = "explicit_opt_out"
            if merchant_id:
                self.store.suppress_merchant(merchant_id, now, opted_out=True)
            return {"action": "end", "rationale": "Explicit opt-out honored immediately; future sends are suppressed."}

        if self._contains(clean, AUTO_REPLY):
            seen = self.store.merchant_state(merchant_id).note_autoreply(text) if merchant_id else 1
            conversation.autoreply_streak += 1
            if seen >= 3 or conversation.autoreply_streak >= 3:
                conversation.state, conversation.stage = "closed", "closed"
                conversation.close_reason = "repeated_auto_reply"
                return {"action": "end", "rationale": "Repeated canned auto-reply with no human engagement; closing without another nudge."}
            wait = (config.AUTOREPLY_BACKOFF_SECONDS if seen == 1
                    else config.AUTOREPLY_LONG_BACKOFF_SECONDS)
            conversation.wait_until = now + timedelta(seconds=wait)
            if merchant_id:
                self.store.hold_merchant(merchant_id, now, wait)
            return {"action": "wait", "wait_seconds": wait,
                    "rationale": "Detected a canned auto-reply; backing off rather than creating a reply loop."}

        conversation.autoreply_streak = 0
        if self._contains(clean, REJECT):
            conversation.state, conversation.stage = "closed", "closed"
            conversation.close_reason = "declined"
            return {"action": "end", "rationale": "The proposal was declined; ending without pressure."}
        if self._contains(clean, WAIT_WORDS):
            wait = 1800
            conversation.wait_until = now + timedelta(seconds=wait)
            return {"action": "wait", "wait_seconds": wait,
                    "rationale": "The recipient asked for space; waiting before any continuation."}

        if self._contains(clean, OFF_TOPIC):
            pending = self._pending(conversation)
            return self._send_reply(
                conversation,
                f"That is outside what I can handle, so your CA or relevant specialist is the right person. Coming back to this: shall I {pending}?",
                YES_NO,
                "Declined an out-of-scope request and returned to the grounded next step with one binary ask.",
                now,
            )

        accepted = self._accepted(clean)
        if accepted:
            pending = self._pending(conversation)
            conversation.committed = True
            conversation.stage = "committed"
            body = self._fulfilment(conversation, pending)
            response = self._send_reply(
                conversation, body, OPEN,
                "Detected explicit commitment and switched from qualification to immediate execution.", now,
            )
            conversation.stage = "delivered"
            if merchant_id:
                self.store.hold_merchant(merchant_id, now, config.POST_SEND_QUIET_SECONDS)
            return response

        if "?" in text:
            pending = self._pending(conversation)
            return self._send_reply(
                conversation,
                f"I can help with the part tied to this message. The useful next move is to {pending}; shall I prepare that now?",
                YES_NO,
                "Answered within Vera's scope and reduced the decision to one concrete next step.",
                now,
            )

        pending = self._pending(conversation)
        return self._send_reply(
            conversation, f"Understood. Shall I {pending} now?", YES_NO,
            "Acknowledged the reply and restated the single pending action without repeating the original message.", now,
        )

    def _eligible(self, action, trigger, now):
        merchant_id = action["merchant_id"]
        state = self.store.merchant_state(merchant_id)
        if state.suppressed(now) or state.sends >= config.MAX_SENDS_PER_MERCHANT_PER_TEST:
            return False
        if state.last_send_at:
            elapsed = (now - state.last_send_at).total_seconds() / 60
            if elapsed < config.MIN_MINUTES_BETWEEN_MERCHANT_SENDS:
                return False
        customer_id = action.get("customer_id")
        if customer_id and state.customer_sends.get(customer_id, 0) >= config.MAX_CUSTOMER_SENDS_PER_TEST:
            return False
        if customer_id and not self.store.get("customer", customer_id):
            return action.get("send_as") == VERA
        if self.store.key_used(action.get("suppression_key")):
            return False
        if self.store.body_already_sent(merchant_id, action.get("body")):
            return False
        expiry = parse_iso(trigger.get("expires_at"))
        return expiry is None or now <= expiry

    @staticmethod
    def _score(trigger, meta, now):
        urgency = max(0, min(5, int(trigger.get("urgency") or 0))) / 5
        value = max(0, min(10, int(meta.get("value") or 0))) / 10
        customer_bonus = 0.08 if trigger.get("customer_id") else 0
        return round(value * 0.68 + urgency * 0.24 + customer_bonus, 4)

    @staticmethod
    def _conversation_id(facts, draft, suppression_key):
        subject = facts.customer_name or facts.owner_first or facts.merchant_id
        topic = draft.get("topic") or facts.kind
        digest = stable_id(facts.merchant_id, facts.customer_id, facts.trigger_id,
                           suppression_key, length=7)
        return f"conv_{slug_token(subject, 18)}_{slug_token(topic, 22)}_{digest}"

    @staticmethod
    def _contains(clean, phrases):
        padded = f" {clean} "
        return any(f" {normalise(phrase)} " in padded for phrase in phrases)

    def _accepted(self, clean):
        if self._contains(clean, REJECT):
            return False
        return self._contains(clean, ACCEPT) or clean in {"1", "2", "yes please", "ok"}

    @staticmethod
    def _pending(conversation):
        topic = conversation.topic if isinstance(conversation.topic, dict) else {}
        return str(topic.get("pending_ask") or "prepare the proposed next step").strip().rstrip(".?")

    @staticmethod
    def _fulfilment(conversation, pending):
        topic = conversation.topic if isinstance(conversation.topic, dict) else {}
        audience = topic.get("audience")
        if audience == "customer":
            return "Confirmed. The merchant team has your reply and will take the next step from here."
        return (f"On it. I have moved this to execution: {pending}. "
                "I will keep it grounded in the details already on file and bring back the finished draft for approval.")

    @staticmethod
    def _send_reply(conversation, body, cta, rationale, now):
        if conversation.already_said(body):
            conversation.state, conversation.stage = "closed", "closed"
            return {"action": "end", "rationale": "The next response would repeat an earlier turn, so the conversation is closed."}
        conversation.add_outbound(body, now.isoformat())
        conversation.stage = "engaged"
        return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def compose(category, merchant, trigger, customer=None, now=None):
    """Compose one action directly from the four challenge contexts.

    This small adapter is useful for unit tests and local integrations; the HTTP
    server uses :class:`Engine` so it can retain suppression and conversation state.
    """
    store = Store()
    merchant = merchant if isinstance(merchant, dict) else {}
    category = category if isinstance(category, dict) else {}
    trigger = trigger if isinstance(trigger, dict) else {}
    customer = customer if isinstance(customer, dict) else None
    merchant_id = merchant.get("merchant_id") or trigger.get("merchant_id") or "merchant"
    category_id = category.get("slug") or merchant.get("category_slug") or "category"
    trigger_id = trigger.get("id") or "trigger"
    store.put_context("category", category_id, 1, category)
    store.put_context("merchant", merchant_id, 1, merchant)
    if customer:
        customer_id = customer.get("customer_id") or trigger.get("customer_id") or "customer"
        store.put_context("customer", customer_id, 1, customer)
    else:
        customer_id = trigger.get("customer_id")
    store.put_context("trigger", trigger_id, 1, trigger)
    result, reason = Engine(store).compose(trigger, resolve_now(now))
    if result is None:
        return {
            "body": "",
            "cta": NONE,
            "send_as": VERA,
            "suppression_key": trigger.get("suppression_key"),
            "rationale": reason,
        }
    action, _ = result
    return {
        "body": action["body"],
        "cta": action["cta"],
        "send_as": action["send_as"],
        "suppression_key": action["suppression_key"],
        "rationale": action["rationale"],
    }
