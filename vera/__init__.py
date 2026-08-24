"""Vera — merchant messaging engine for the magicpin AI Challenge.

Deterministic composer: no model call in the request path, so every response is
reproducible, sub-millisecond, and structurally incapable of hallucinating a fact
that is not in the pushed context.
"""

__version__ = "1.1.0"
