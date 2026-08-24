# Vera Signal Foundry

A deterministic, stateful message engine for the magicpin Vera AI Challenge.

## Approach

The bot converts each pushed category, merchant, trigger, and optional customer context into a provenance-tracked fact sheet. Trigger-specific playbooks choose the highest-value next move, while category voice, consent, live offers, performance, seasonality, conversation history, and customer preferences shape the message.

Every proactive send passes hard guards for unsupported numbers, taboo claims, leaked internal identifiers, duplicate bodies, duplicate suppression keys, expiry, frequency, and multiple questions. The engine intentionally returns no action when it cannot say something specific and grounded.

No LLM runs in the request path. This makes the same context and simulated time produce the same decision and message, avoids prompt instability, and keeps responses fast under concurrent judge traffic.

## Run

Python 3.9 or newer is sufficient; there are no third-party dependencies.

```bash
python bot.py
```

The server listens on `0.0.0.0:8080` by default. Override with `PORT`, `VERA_HOST`, or `VERA_PORT`.

## Endpoints

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `POST /v1/teardown`

## Deployment

Deploy the directory as a Python web service with start command `python bot.py`. The process must remain alive for the full evaluation because context and conversations are intentionally held only in memory and are erased by `/v1/teardown`.

Set `VERA_TEAM_NAME`, `VERA_TEAM_MEMBERS`, and `VERA_CONTACT_EMAIL` in the host environment if the values in `identity.json` need changing.

## Tradeoffs

The rule engine prioritizes reliability, grounding, and decision transparency over open-ended prose generation. Unknown trigger kinds fall back only to merchant signals or category intelligence actually present in context; otherwise Vera stays silent. Reply handling is intent-based and deterministic, covering acceptance, rejection, delay, auto-replies, opt-out, and off-topic requests without pretending an external task has already completed.
