"""In-memory state: versioned context store, conversations, suppression ledger.

Nothing is persisted to disk. ``POST /v1/teardown`` wipes everything, which is
also what the challenge's privacy rule asks for ("must not persist context after
the test window").

Idempotency (challenge-testing-brief.md 2.1 vs api-call-examples.md 1.5 disagree;
resolution documented in README):

* first push of a context id            -> 200 ``accepted: true``
* strictly higher version               -> 200 ``accepted: true``, replaces atomically
* identical version re-pushed           -> 200 ``accepted: true, no_op: true``
* strictly lower version                -> 409 ``accepted: false, reason: stale_version``

The normative section of the testing brief calls an identical re-push "a no-op",
and the shipped judge simulator marks any response without a truthy ``accepted``
as FAIL during warmup — where a failed warmup means disqualification. So an equal
version acks with ``accepted: true`` and an explicit ``no_op`` flag, and only a
genuinely stale version is refused.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from . import config
from .util import UTC, normalise_tight, now_utc_iso, parse_iso

SCOPES = ("category", "merchant", "customer", "trigger")


class Conversation:
    """One thread with one counterparty. Turn history drives every reply decision."""

    __slots__ = ("conversation_id", "merchant_id", "customer_id", "trigger_id",
                 "topic", "state", "stage", "turns", "bot_bodies", "autoreply_streak",
                 "opened_at", "last_bot_at", "wait_until", "close_reason",
                 "declined_topics", "committed")

    def __init__(self, conversation_id, merchant_id=None, customer_id=None,
                 trigger_id=None, topic=None, opened_at=None):
        self.conversation_id = conversation_id
        self.merchant_id = merchant_id
        self.customer_id = customer_id
        self.trigger_id = trigger_id
        self.topic = topic or {}
        self.state = "open"
        self.stage = "opened"        # opened -> engaged -> committed -> delivered -> closed
        self.turns = []              # [{"role": "vera"|"merchant"|"customer", "text": str, "at": iso}]
        self.bot_bodies = []         # normalised bodies, for the anti-repetition guard
        self.autoreply_streak = 0
        self.opened_at = opened_at or now_utc_iso()
        self.last_bot_at = None
        self.wait_until = None
        self.close_reason = None
        self.declined_topics = []
        self.committed = False

    # -- turn bookkeeping ------------------------------------------------
    def add_inbound(self, role, text, at=None):
        self.turns.append({"role": role or "merchant", "text": text or "", "at": at or now_utc_iso()})

    def add_outbound(self, body, at=None):
        self.turns.append({"role": "vera", "text": body or "", "at": at or now_utc_iso()})
        if body:
            self.bot_bodies.append(normalise_tight(body))
        self.last_bot_at = at or now_utc_iso()

    def already_said(self, body) -> bool:
        return normalise_tight(body) in self.bot_bodies

    def last_bot_body(self):
        for turn in reversed(self.turns):
            if turn["role"] == "vera":
                return turn["text"]
        return None

    def inbound_count(self) -> int:
        return sum(1 for turn in self.turns if turn["role"] != "vera")

    def snapshot(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "merchant_id": self.merchant_id,
            "customer_id": self.customer_id,
            "trigger_id": self.trigger_id,
            "state": self.state,
            "stage": self.stage,
            "turns": len(self.turns),
            "topic": self.topic.get("label") if isinstance(self.topic, dict) else None,
        }


class MerchantState:
    """Per-merchant guard rails that outlive any single conversation."""

    __slots__ = ("merchant_id", "sends", "last_send_at", "customer_sends", "used_keys",
                 "bodies", "topics", "opted_out", "opt_out_at", "suppress_until",
                 "autoreply_texts", "engaged_at", "conversations")

    def __init__(self, merchant_id):
        self.merchant_id = merchant_id
        self.sends = 0
        self.last_send_at = None            # datetime, simulated clock
        self.customer_sends = {}            # customer_id -> count
        self.used_keys = set()              # suppression keys already spent
        self.bodies = set()                 # normalised bodies ever sent to this merchant
        self.topics = set()                 # topic keys already covered
        self.opted_out = False
        self.opt_out_at = None
        self.suppress_until = None          # datetime
        self.autoreply_texts = {}           # normalised canned text -> times seen
        self.engaged_at = None
        self.conversations = []             # conversation ids, newest last

    def suppressed(self, now: datetime) -> bool:
        if self.opted_out:
            return True
        return self.suppress_until is not None and now < self.suppress_until

    def note_autoreply(self, text) -> int:
        key = normalise_tight(text)[:160]
        if not key:
            return 0
        self.autoreply_texts[key] = self.autoreply_texts.get(key, 0) + 1
        return self.autoreply_texts[key]

    def autoreply_seen(self, text) -> int:
        return self.autoreply_texts.get(normalise_tight(text)[:160], 0)


class Store:
    """Thread-safe. Every mutation happens under one lock; reads are cheap copies."""

    def __init__(self):
        self._lock = threading.RLock()
        self.started_at = time.monotonic()
        self.contexts = {scope: {} for scope in SCOPES}
        self.conversations = {}
        self.merchants_state = {}
        self.suppression_ledger = {}         # suppression_key -> iso first used
        self.stats = {"context_pushes": 0, "context_conflicts": 0, "context_no_ops": 0,
                      "ticks": 0, "replies": 0, "actions_sent": 0, "holds": 0,
                      "validator_rewrites": 0, "validator_blocks": 0}

    # ---------------------------------------------------------------- contexts
    def put_context(self, scope, context_id, version, payload, delivered_at=None):
        """Returns ``(http_status, response_dict)``. See the module docstring."""
        with self._lock:
            bucket = self.contexts[scope]
            existing = bucket.get(context_id)
            stored_at = now_utc_iso()
            ack = f"ack_{context_id}_v{version}"

            if existing is not None and version < existing["version"]:
                self.stats["context_conflicts"] += 1
                return 409, {"accepted": False, "reason": "stale_version",
                             "current_version": existing["version"],
                             "context_id": context_id, "scope": scope}

            no_op = existing is not None and version == existing["version"]
            if not no_op:
                bucket[context_id] = {
                    "version": version,
                    "payload": payload,
                    "stored_at": stored_at,
                    "delivered_at": delivered_at,
                    "replaces": existing["version"] if existing else None,
                }
                self.stats["context_pushes"] += 1
            else:
                self.stats["context_no_ops"] += 1

            response = {"accepted": True, "ack_id": ack, "stored_at": stored_at,
                        "scope": scope, "context_id": context_id,
                        "current_version": bucket[context_id]["version"]}
            if no_op:
                response["no_op"] = True
                response["reason"] = "duplicate_version"
            elif existing is not None:
                response["replaced_version"] = existing["version"]
            return 200, response

    def get(self, scope, context_id):
        if context_id is None:
            return None
        with self._lock:
            record = self.contexts.get(scope, {}).get(context_id)
            return record["payload"] if record else None

    def version_of(self, scope, context_id):
        with self._lock:
            record = self.contexts.get(scope, {}).get(context_id)
            return record["version"] if record else None

    def counts(self) -> dict:
        with self._lock:
            return {scope: len(self.contexts[scope]) for scope in SCOPES}

    def category_for_merchant(self, merchant_id):
        merchant = self.get("merchant", merchant_id)
        if not isinstance(merchant, dict):
            return None, None
        slug = merchant.get("category_slug") or merchant.get("category")
        return slug, self.get("category", slug)

    def all_trigger_ids(self):
        with self._lock:
            return sorted(self.contexts["trigger"].keys())

    def trigger(self, trigger_id):
        """Triggers may be pushed with the envelope id or the payload's own id."""
        payload = self.get("trigger", trigger_id)
        if isinstance(payload, dict):
            return payload
        with self._lock:
            for record in self.contexts["trigger"].values():
                inner = record["payload"]
                if isinstance(inner, dict) and inner.get("id") == trigger_id:
                    return inner
        return None

    # ---------------------------------------------------------------- merchants
    def merchant_state(self, merchant_id) -> MerchantState:
        with self._lock:
            state = self.merchants_state.get(merchant_id)
            if state is None:
                state = MerchantState(merchant_id)
                self.merchants_state[merchant_id] = state
            return state

    # ---------------------------------------------------------------- conversations
    def conversation(self, conversation_id):
        with self._lock:
            return self.conversations.get(conversation_id)

    def ensure_conversation(self, conversation_id, merchant_id=None, customer_id=None,
                            trigger_id=None, topic=None) -> Conversation:
        with self._lock:
            conversation = self.conversations.get(conversation_id)
            if conversation is None:
                conversation = Conversation(conversation_id, merchant_id, customer_id,
                                            trigger_id, topic)
                self.conversations[conversation_id] = conversation
                if merchant_id:
                    state = self.merchant_state(merchant_id)
                    if conversation_id not in state.conversations:
                        state.conversations.append(conversation_id)
            else:
                conversation.merchant_id = conversation.merchant_id or merchant_id
                conversation.customer_id = conversation.customer_id or customer_id
                conversation.trigger_id = conversation.trigger_id or trigger_id
                if topic and not conversation.topic:
                    conversation.topic = topic
            return conversation

    def latest_conversation_for(self, merchant_id):
        """Most recent live thread for a merchant — used to resume a topic."""
        with self._lock:
            state = self.merchants_state.get(merchant_id)
            if not state:
                return None
            for conversation_id in reversed(state.conversations):
                conversation = self.conversations.get(conversation_id)
                if conversation and conversation.state != "closed":
                    return conversation
            return None

    # ---------------------------------------------------------------- sending
    def record_send(self, action: dict, now: datetime, topic_key=None):
        """Book a composed action against every relevant guard rail."""
        merchant_id = action.get("merchant_id")
        with self._lock:
            self.stats["actions_sent"] += 1
            key = action.get("suppression_key")
            if key:
                self.suppression_ledger.setdefault(key, now_utc_iso(now))
            if not merchant_id:
                return
            state = self.merchant_state(merchant_id)
            state.sends += 1
            state.last_send_at = now
            if key:
                state.used_keys.add(key)
            body = action.get("body")
            if body:
                state.bodies.add(normalise_tight(body))
            if topic_key:
                state.topics.add(topic_key)
            customer_id = action.get("customer_id")
            if customer_id:
                state.customer_sends[customer_id] = state.customer_sends.get(customer_id, 0) + 1
            conversation = self.ensure_conversation(
                action.get("conversation_id"), merchant_id, customer_id,
                action.get("trigger_id"))
            conversation.add_outbound(body, now_utc_iso(now))
            if topic_key and not conversation.topic:
                conversation.topic = {"key": topic_key}

    def key_used(self, suppression_key) -> bool:
        with self._lock:
            return suppression_key in self.suppression_ledger

    def body_already_sent(self, merchant_id, body) -> bool:
        with self._lock:
            state = self.merchants_state.get(merchant_id)
            return bool(state and normalise_tight(body) in state.bodies)

    def suppress_merchant(self, merchant_id, now: datetime, days=None, opted_out=False):
        with self._lock:
            state = self.merchant_state(merchant_id)
            if opted_out:
                state.opted_out = True
                state.opt_out_at = now_utc_iso(now)
            span = config.OPT_OUT_SUPPRESSION_DAYS if days is None else days
            until = now + timedelta(days=span)
            if state.suppress_until is None or until > state.suppress_until:
                state.suppress_until = until

    def hold_merchant(self, merchant_id, now: datetime, seconds: int):
        with self._lock:
            state = self.merchant_state(merchant_id)
            until = now + timedelta(seconds=max(0, int(seconds)))
            if state.suppress_until is None or until > state.suppress_until:
                state.suppress_until = until

    def bump(self, stat, amount=1):
        with self._lock:
            self.stats[stat] = self.stats.get(stat, 0) + amount

    # ---------------------------------------------------------------- lifecycle
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def health(self) -> dict:
        with self._lock:
            return {
                "status": "ok",
                "uptime_seconds": self.uptime_seconds(),
                "contexts_loaded": {scope: len(self.contexts[scope]) for scope in SCOPES},
                "conversations": len(self.conversations),
                "version": config.VERSION,
            }

    def diagnostics(self) -> dict:
        with self._lock:
            return {
                "contexts_loaded": {scope: len(self.contexts[scope]) for scope in SCOPES},
                "conversations": [c.snapshot() for c in self.conversations.values()],
                "suppression_keys": sorted(self.suppression_ledger.keys()),
                "merchants_touched": {
                    mid: {"sends": st.sends, "opted_out": st.opted_out,
                          "topics": sorted(st.topics)}
                    for mid, st in sorted(self.merchants_state.items()) if st.sends or st.opted_out
                },
                "stats": dict(self.stats),
            }

    def teardown(self) -> dict:
        """Wipe all delivered context and conversation state."""
        with self._lock:
            wiped = {scope: len(self.contexts[scope]) for scope in SCOPES}
            wiped["conversations"] = len(self.conversations)
            self.contexts = {scope: {} for scope in SCOPES}
            self.conversations = {}
            self.merchants_state = {}
            self.suppression_ledger = {}
            for stat in self.stats:
                self.stats[stat] = 0
            return {"wiped": True, "removed": wiped, "at": now_utc_iso()}


def resolve_now(raw) -> datetime:
    """The simulated clock from a request, falling back to real time."""
    return parse_iso(raw) or datetime.now(UTC)
