"""A structural guard for the README's blanket fencing promise.

Every other fencing test in this suite names one field. That is fine for
regressions, but it can only ever confirm the fields somebody remembered to
write a test for, so the guarantee ends up enforced by attention rather than by
the suite. Three fields have slipped through that way already: `headline` (#6),
`author_headline` (#13) and `company.tagline` (#18). Each was found by reading
the code, and a fourth would be found the same way.

This test asks a different question: does any prose-shaped field anywhere in the
tool surface reach a caller unfenced? It works by inspecting source rather than
by driving every tool, because driving them all would need a fixture per page
layout and would still only cover the paths those fixtures happen to exercise.

The allowlist below is the important half. Adding a field to it is a deliberate
statement that the field is structural, and that statement is visible in review.
Silently returning a new prose field is what this is here to stop.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "linkedin_mcp"
# safety.py defines fence(); walking it would flag the implementation itself.
SKIP_FILES = {"safety.py"}

# Keys whose values are prose a stranger authored. These must be fenced.
PROSE_KEYS = {
    "about",
    "description",
    "headline",
    "author_headline",
    "tagline",
    "summary",
    "snippet",
    "text",
    "body",
    "message",
    "preview",
    "last_message_preview",
}

# Keys that look textual but are structural, and are deliberately returned
# plain. Each entry is a decision, not an oversight.
#
#   name, author_name   short identifiers a caller matches on. Fencing them
#                       would make every result awkward to use for no gain.
#   title               a job or role title, a short label rather than prose.
#   organisation,       employer and school names, matched against and linked.
#   school, degree
#   industry, location, short enumerated-ish values from LinkedIn's own
#   headquarters,       taxonomy, not free composition.
#   size, founded,
#   website, dates
#   participant_name    a person's name in an inbox row.
STRUCTURAL_KEYS = {
    "name",
    "author_name",
    "participant_name",
    "title",
    "organisation",
    "school",
    "degree",
    "industry",
    "location",
    "headquarters",
    "size",
    "founded",
    "website",
    "dates",
    "url",
    "slug",
    "public_id",
    "profile_url",
    "posted",
    "timestamp",
}

# `"key": value` at the start of a returned dict entry, capturing the key and
# the rest of the line so we can look for a fence on it.
ENTRY = re.compile(r'^\s*"(?P<key>[a-z_]+)"\s*:\s*(?P<value>.+)$')


def _unfenced_prose_fields() -> list[str]:
    """Return "file:line key" for every prose field returned without fence().

    `message` is the one genuinely ambiguous key. It holds a LinkedIn message
    body in `get_conversation`, which is untrusted, and it holds our own text in
    the house error shape `{"error": True, "kind": ..., "message": ...}`, which
    is not. The two are told apart by looking back a few lines for the `error`
    marker: fencing our own error strings would be noise, and worse, it would
    make a real unfenced message body look handled.
    """
    offenders: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name in SKIP_FILES:
            continue
        rel = path.relative_to(SRC_DIR)
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines, start=1):
            m = ENTRY.match(line)
            if not m:
                continue
            key = m.group("key")
            if key not in PROSE_KEYS:
                continue
            preceding = "\n".join(lines[max(0, i - 6) : i - 1])
            if '"error": True' in preceding:
                continue
            # fence() may open on this line and close several lines later, so
            # look at a small window rather than the single line.
            window = "\n".join(lines[i - 1 : i + 4])
            if "fence(" in window:
                continue
            offenders.append(f"{rel}:{i} {key}")
    return offenders


def test_every_prose_field_is_fenced():
    """No prose-shaped field reaches a caller without a fence.

    If this fails, either fence the field or, if it is genuinely structural,
    add it to STRUCTURAL_KEYS with a comment saying why. Both are fine. Doing
    neither is what this test exists to prevent.
    """
    offenders = _unfenced_prose_fields()
    assert not offenders, (
        "prose fields returned without fence():\n  "
        + "\n  ".join(offenders)
        + "\n\nFence them, or add the key to STRUCTURAL_KEYS with a reason."
    )


def test_the_two_key_sets_do_not_overlap():
    """A key cannot be both prose and structural.

    Guards against someone silencing a failure by adding a genuinely
    prose-shaped key to the structural list without removing it from PROSE_KEYS,
    which would leave the classification self-contradictory and the test green.
    """
    assert not (PROSE_KEYS & STRUCTURAL_KEYS)


def test_the_guard_actually_detects_an_unfenced_field(tmp_path, monkeypatch):
    """The detector must fail on a planted unfenced field.

    Without this, a bug in the regex would make the guard silently vacuous:
    it would find nothing and report success forever, which is exactly the
    failure mode it was written to eliminate.
    """
    fake_src = tmp_path / "linkedin_mcp"
    fake_src.mkdir()
    (fake_src / "planted.py").write_text(
        'def build():\n    return {\n        "headline": raw_headline,\n    }\n'
    )
    monkeypatch.setattr("tests.test_fencing_coverage.SRC_DIR", fake_src)

    assert any("headline" in o for o in _unfenced_prose_fields())
