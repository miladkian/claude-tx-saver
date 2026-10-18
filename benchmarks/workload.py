"""Deterministic synthetic code/log context; never reads user project data."""

import json
import random
from pathlib import Path

import aiohttp
import tiktoken


def public_corpus(token_count=100_000):
    """Actual installed open-source HTTP client code, not generated repetition."""
    root = Path(aiohttp.__file__).resolve().parent
    text = "".join("\n# File: aiohttp/" + p.name + "\n" + p.read_text()
                   for p in sorted(root.glob("*.py")))
    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    if len(tokens) < token_count:
        raise ValueError("public source fixture is too short")
    text = encoding.decode(tokens[:token_count])
    assert len(encoding.encode(text)) == token_count
    return text


def corpus(token_count=100_000, seed=1927):
    rng = random.Random(seed)
    encoding = tiktoken.get_encoding("cl100k_base")
    sections = []
    # Mixed source, diagnostics, and documentation, with varied identifiers/data.
    for i in range(token_count // 65 + 100):
        ident = rng.randbytes(6).hex()
        sections.append(f'''# File: services/worker_{i:04d}_{ident}.py
def process_{ident}(records, retry_limit={rng.randrange(2, 9)}):
    """Validate tenant records, preserve ordering, and record delivery failures.
    A failed transaction must roll back before the queue retries its batch.
    Request identifiers are unique; no production data appears in this fixture.
    """
    accepted = []
    for record in records:
        if record.get("tenant_id") == "tenant-{rng.randrange(100000)}":
            accepted.append((record["id"], record.get("value", {rng.randrange(1000)})))
    return sorted(accepted)
# test result: worker={i}, checksum={rng.randbytes(16).hex()}, duration_ms={rng.randrange(1, 500)}
# Trace: validate -> transaction -> commit -> acknowledge; status=passed

''')
    tokens = encoding.encode("".join(sections))
    if len(tokens) < token_count:
        raise ValueError("generated corpus is too short")
    text = encoding.decode(tokens[:token_count])
    assert len(encoding.encode(text)) == token_count
    return text


def request_body(context, extra="", metadata="stable-session"):
    return json.dumps({
        "model": "claude-haiku-4-5", "max_tokens": 4, "stream": True,
        "system": "Read the supplied synthetic context and reply OK.",
        "metadata": {"user_id": metadata},
        "messages": [{"role": "user", "content": context}] + (
            [{"role": "assistant", "content": "OK"}, {"role": "user", "content": extra}] if extra else []
        ),
    }, separators=(",", ":")).encode()
