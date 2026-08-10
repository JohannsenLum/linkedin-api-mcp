# Changelog

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
