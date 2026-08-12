# Changelog

## [1.0.0] - 2026-08-10

First stable release. The tool surface, the return shapes and the safety
guarantees are now a contract, and a breaking change to any of them means 2.0.0.

### Security

- **Headlines are fenced as untrusted data.** A LinkedIn headline is free text any
  stranger can write, and it was reaching a model that also holds `send_message`
  and `connect`. The README already promised that human-written text is fenced and
  `about` was, but `headline` was not, so the guarantee was only partly true.

  Now fenced at all three sites, `get_profile`, the `search_people` structural row
  path, and the selector fallback, through the same `clean`, `truncate`, `fence`
  pipeline as `about`, with `fence` outermost so its boundary nonce is still
  generated after the content exists. `_HEADLINE_LIMIT` is 220, matching
  LinkedIn's own headline cap, so truncation never fires on a real headline.

  Contributed by @VedantMadane in #12, closing #6.

- **Regression tests now guard the fencing.** Previously only the `get_profile`
  site was covered, so the two `search_people` paths could have been silently
  unfenced by a refactor with the suite staying green. All three paths are now
  tested, and the fence label is pinned as well as the wrapper, since a
  mislabelled provenance marker is how a reader later mistakes one field for
  another. `tests/fakes.py` gained a search-results fixture that future search
  tests can reuse.

### Added

- **Contributing docs explain how to run the tests.** The default suite needs no
  Chromium, no LinkedIn account, no cookie and no network access, which was true
  but written down nowhere. Contributed by @averyquinnhq in #11, closing #7.

### Changed

- **Breaking:** `headline` is now a fenced string rather than a raw value in
  `get_profile` and `search_people`. Callers matching on exact content must match
  on containment instead.

## [0.0.4] - 2026-08-10

### Fixed

- Post author headlines are now fenced as untrusted content, matching the
  existing protection for post bodies and preventing headline-based prompt
  injection from being presented as trusted tool output.

- **`experience` and `education` are no longer empty.** The assumption was that
  they lazy-load on scroll. That was wrong: scrolling six screens leaves the page
  text byte-identical. They are not on the profile page at all. They live at
  `/in/<id>/details/experience/` and `/details/education/`, fully rendered, and one
  navigation gets the lot.

  Entries are found with the same rule as search results: an entry is the smallest
  ancestor holding exactly one company or school link. The date range is the field
  with a recognisable shape, a year plus a dash or "Present", so what precedes it
  is title and organisation and what follows is location and description.

  Education is labelled separately because LinkedIn lists the school first and the
  qualification second, the reverse of experience. Naming by position would have
  silently swapped them.

  Verified live: four roles with dates and locations, two schools with degrees.
  Costs one extra navigation per section, so the caller asks for them rather than
  paying for them by default.

### Added

- One-click install buttons for Claude Code, Claude Desktop, Cursor, VS Code, Zed,
  Windsurf and Codex CLI. The Cursor and VS Code deeplink payloads were decoded and
  checked to produce exactly `uvx linkedin-api-mcp`, since a malformed one silently
  installs a broken server.
- An animated terminal header showing a people search, then a message being sent
  and confirmed by reading the thread back.

## [0.0.3] - 2026-08-10

The first version whose parsers have actually been run against LinkedIn.
0.0.1 and 0.0.2 returned parse_failed on every page.

### Fixed

- **Every read tool now works.** LinkedIn ships hashed per-build CSS class
  names (`b0712e9a`, `_129ac5aa`), has removed the `id="experience"` section
  anchors, and serves profile pages with zero `h1` elements. Every selector
  naming a class was dead. Parsing now anchors on what a rebuild cannot
  rename: `document.title`, `href` patterns, and structure. A result row is
  the smallest ancestor containing exactly one entity link, and fields are
  classified by shape rather than position.
- **`send_message` no longer reports success for messages it did not send.**
  Both send paths clicked a button and returned. Nothing checked that the
  message arrived, so a wrong-but-clickable element produced `{"sent": true}`
  for a message that never existed. Both paths now poll until the text
  appears in the conversation and raise `send_unconfirmed` otherwise.
- **Compose is reached by navigating, not clicking.** LinkedIn ignores
  synthetic clicks on the Message control, and a trusted click times out on
  the overlay. It is an `<a>` pointing at `/messaging/compose/?recipient=`,
  so the href is navigated instead. Two decoys sit beside it: links carrying
  a prefilled `body=` template, and links aimed at a different person. Both
  are now rejected, since either would have sent the wrong thing to someone.
- **A malformed cookie is named as such.** It sends LinkedIn into a redirect
  loop, which was reported as `navigation_failed` with a hint to check your
  network. It now says the cookie is malformed, which is what it means.
- **`auth` works without a terminal.** `getpass` raised an unhandled
  `EOFError` in any piped or containerised shell. It now reads piped input,
  so `pbpaste | linkedin-api-mcp auth` works.
- The profile top card no longer picks the wrong field for a short headline.
  Classifying by length returned the organisation line for anyone whose
  headline is under 25 characters.

### Known gaps

- `conversation_id` is `null` from the inbox: LinkedIn binds each row's click
  to an in-memory object rather than a URL, so there is nothing to read.
- Post reaction and comment counts return `null`.
- `experience` and `education` load only on scroll, and are still empty.
