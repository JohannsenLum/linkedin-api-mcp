# Changelog

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

