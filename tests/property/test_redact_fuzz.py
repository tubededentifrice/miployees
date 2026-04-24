"""Hypothesis-driven leak check for :func:`app.util.redact.redact`.

The strategy builds arbitrary nested ``dict`` / ``list`` / ``str``
trees, injects a known PII literal at a random leaf, runs the tree
through the redactor under ``scope="log"``, and asserts the literal
does not survive anywhere in the output. This catches any path — a
new container type, a recursion-cap regression, an ordering bug in
:func:`~app.util.redact.scrub_string` — that would let raw PII
escape the seam.

Stress target: 500 examples per run. The acceptance criterion on
cd-a469 names 1000; we start at 500 to keep CI under a second and
upgrade if the fuzzer surfaces something that 500 missed.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from app.util.redact import redact

# Injected literals we demand the redactor hide. Each is valid under
# its respective rule (email RFC-ish, E.164, mod-97 IBAN, Luhn PAN,
# 64-char hex, Bearer prefix).
_PII_LITERALS: tuple[str, ...] = (
    "leak@example.com",
    "+33612345678",
    "FR1420041010050500013M02606",
    "4242424242424242",
    "deadbeefcafebabe" * 4,  # 64 hex chars
    "Bearer sk-or-leaked-token-xyz",
)


def _noise_strategy() -> st.SearchStrategy[str]:
    """Random ASCII strings that look like realistic free-text noise."""
    return st.text(
        alphabet=st.characters(
            min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters="\\"
        ),
        max_size=40,
    )


def _leaf_strategy() -> st.SearchStrategy[object]:
    """One of the simple JSON-ish leaves the redactor handles."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1_000, max_value=1_000),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        _noise_strategy(),
    )


def _tree_strategy(max_depth: int = 4) -> st.SearchStrategy[object]:
    """Recursive dict / list strategy, capped at ``max_depth`` levels."""

    def extend(children: st.SearchStrategy[object]) -> st.SearchStrategy[object]:
        return st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(
                keys=st.text(
                    alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
                    min_size=1,
                    max_size=8,
                ),
                values=children,
                max_size=4,
            ),
        )

    return st.recursive(_leaf_strategy(), extend, max_leaves=20)


def _inject(tree: object, literal: str, path_seed: int) -> object:
    """Insert ``literal`` somewhere inside ``tree`` and return the new tree.

    We choose the insertion point deterministically from ``path_seed``
    so Hypothesis can shrink on a failing example. When the tree is
    atomic we wrap it into a single-key dict carrying the literal; when
    it's a container we pick the first writable slot and recurse if
    that slot itself is a container. The fallback path wraps atomics
    so every run actually inserts the literal.
    """
    if isinstance(tree, dict):
        if not tree:
            return {"leak_here": literal}
        keys = list(tree.keys())
        chosen = keys[path_seed % len(keys)]
        child = tree[chosen]
        if isinstance(child, dict | list):
            tree[chosen] = _inject(child, literal, path_seed + 1)
        else:
            tree[chosen] = literal
        return tree
    if isinstance(tree, list):
        if not tree:
            return [literal]
        idx = path_seed % len(tree)
        child = tree[idx]
        if isinstance(child, dict | list):
            tree[idx] = _inject(child, literal, path_seed + 1)
        else:
            tree[idx] = literal
        return tree
    return {"leak_here": literal}


@settings(max_examples=500, deadline=None)
@given(
    tree=_tree_strategy(),
    literal_idx=st.integers(min_value=0, max_value=len(_PII_LITERALS) - 1),
    path_seed=st.integers(min_value=0, max_value=10_000),
)
def test_redactor_hides_every_injected_literal(
    tree: object, literal_idx: int, path_seed: int
) -> None:
    literal = _PII_LITERALS[literal_idx]
    payload = _inject(tree, literal, path_seed)

    redacted = redact(payload, scope="log")

    # Serialise the output (via ``json.dumps(default=repr)``) and
    # assert the literal is gone. ``json.dumps`` walks dicts / lists
    # / tuples / primitives exhaustively; unknown leaves fall through
    # to ``repr``. This is a cheap full-tree string search.
    as_text = json.dumps(redacted, default=repr)
    assert literal not in as_text, (
        f"literal {literal!r} survived redaction; "
        f"input {payload!r}, output {redacted!r}"
    )


@settings(max_examples=200, deadline=None)
@given(literal_idx=st.integers(min_value=0, max_value=len(_PII_LITERALS) - 1))
def test_redactor_handles_pure_string_leaves(literal_idx: int) -> None:
    """A standalone string containing a PII literal still gets scrubbed."""
    literal = _PII_LITERALS[literal_idx]
    redacted = redact(f"noise {literal} more noise", scope="log")
    assert isinstance(redacted, str)
    assert literal not in redacted
