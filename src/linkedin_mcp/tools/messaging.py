"""Inbox and conversations: read the messaging surface, and the two write actions
that go with it (sending a message, sending a connection request).

This is the highest-risk *reading* surface in the server: unlike a profile's
"About" section, which the profile owner wrote about themselves, a message body
is text chosen by whoever is on the other end of the conversation, addressed
directly at "the reader". Every message body, message preview, and search
snippet that leaves this module goes through `safety.fence()` before being
returned, so a model consuming the tool output sees it unambiguously marked as
data, never as something to obey. See `safety.py`'s module docstring for the
full threat model.

It is also the module with the most write tools (`do_send_message`,
`do_connect`), so the refusal paths (empty message, ambiguous target, note too
long, invitation limit) matter as much as the happy path: a write tool that
guesses instead of refusing is how a message goes to the wrong person.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import LinkedInError, not_found, parse_failed
from ..safety import clean, fence, public_id, truncate
from ..session import Session
from ..throttle import ActionQueue, RateLimited

# LinkedIn does not publish an exact compose-box character cap. This is a
# conservative estimate: refusing a little early beats truncating a message
# silently and sending half of it.
MAX_MESSAGE_CHARS = 8000

# LinkedIn's documented cap on connection-request notes for free accounts.
MAX_NOTE_CHARS = 300

# How much of each free-text field we keep before handing it to fence().
# See safety.truncate(): never silent, always a visible marker.
_PREVIEW_LIMIT = 500
_BODY_LIMIT = 6000
_SNIPPET_LIMIT = 500

_CONVO_ID_INVALID = re.compile(r"[/\s]")


# ---------------------------------------------------------------------------
# Cross-cutting: every do_* function bottoms out here so error handling is
# written once. `queue.run` only serialises and paces; it does not catch
# anything, so this is where "tools return structured errors, they never
# raise into the transport" (rule 3) is actually enforced for this module.
# ---------------------------------------------------------------------------


def _unexpected(tool: str) -> dict:
    return LinkedInError(
        "unexpected",
        f"{tool} hit an unexpected internal error while reading the page.",
        "This is likely a bug in this server rather than anything LinkedIn "
        "returned. Please report it with the tool name: "
        "https://github.com/JohannsenLum/linkedin-api-mcp/issues",
    ).as_dict()


async def _run_tool(queue: ActionQueue, label: str, fn: Any) -> dict:
    try:
        return await queue.run(label, fn)
    except LinkedInError as exc:
        return exc.as_dict()
    except RateLimited as exc:
        # Built from the exception's own typed fields rather than str(exc):
        # RateLimited's message happens to be safe too, but this keeps rule 2
        # ("never interpolate a caught exception into a message") literal.
        mins = max(1, round(exc.retry_after_s / 60))
        return {
            "error": True,
            "kind": "rate_limited",
            "message": (
                f"Rate limit reached: {exc.used} LinkedIn actions in the last "
                f"hour (ceiling {exc.ceiling}). Try again in about {mins} "
                f"minute{'s' if mins != 1 else ''}."
            ),
            "actions_last_hour": exc.used,
            "hourly_ceiling": exc.ceiling,
            "retry_after_seconds": round(exc.retry_after_s, 1),
        }
    except Exception:
        return _unexpected(label)


# ---------------------------------------------------------------------------
# Generic DOM helpers. LinkedIn's messaging markup has shifted several times
# over the years and differs between the full /messaging/ page and profile
# overlay widgets, so every lookup here tries a list of selectors and
# degrades to None on a field, or a typed error on a whole page, rather than
# raising on the first one that doesn't match.
# ---------------------------------------------------------------------------


async def _query_first(scope: Any, selectors: tuple[str, ...]) -> Any:
    for sel in selectors:
        try:
            el = await scope.query_selector(sel)
        except Exception:
            continue
        if el is not None:
            return el
    return None


async def _first_text(scope: Any, selectors: tuple[str, ...]) -> str | None:
    for sel in selectors:
        try:
            node = await scope.query_selector(sel)
            if node is None:
                continue
            text = (await node.inner_text()).strip()
        except Exception:
            continue
        if text:
            return text
    return None


def _thread_id_from_href(href: str | None) -> str | None:
    marker = "/messaging/thread/"
    if not href or marker not in href:
        return None
    tail = href.split(marker, 1)[1].strip("/")
    return tail.split("/")[0].split("?")[0] or None


def _profile_id_from_href(href: str | None) -> str | None:
    if not href or "/in/" not in href:
        return None
    tail = href.split("/in/", 1)[1].strip("/")
    return tail.split("/")[0].split("?")[0] or None


def _clean_conversation_id(conversation_id: str | None) -> str:
    cid = (conversation_id or "").strip()
    if not cid:
        raise LinkedInError(
            "invalid_argument",
            "conversation_id is required.",
            "Call get_inbox or search_conversations first to find a conversation_id.",
        )
    if _CONVO_ID_INVALID.search(cid):
        raise LinkedInError(
            "invalid_argument",
            "conversation_id does not look like a LinkedIn thread id.",
            "Pass the conversation_id exactly as returned by get_inbox or "
            "search_conversations, not a URL or a person's name.",
        )
    return cid


# ---------------------------------------------------------------------------
# Inbox listing
# ---------------------------------------------------------------------------

_CONTAINER_SELECTOR = ".msg-conversations-container"
_LIST_SELECTOR = ".msg-conversations-container__conversations-list, .msg-conversations-container ul"
_CARD_SELECTOR = "li.msg-conversation-listitem, li.msg-conversation-card, div.msg-conversation-listitem"

_NAME_SELECTORS = (
    ".msg-conversation-listitem__participant-names",
    ".msg-conversation-card__participant-names",
    ".msg-conversation-listitem__inbox-badge-text",
    "h3 span[aria-hidden='true']",
)

_PREVIEW_SELECTORS = (
    ".msg-conversation-card__message-snippet",
    ".msg-conversation-listitem__message-snippet",
    "p.msg-conversation-listitem__message-snippet",
)

_TIME_SELECTORS = (
    "time.msg-conversation-listitem__time-stamp",
    "time.msg-conversation-card__time-stamp",
    ".msg-conversation-listitem__time-stamp",
    "time",
)

_UNREAD_BADGE_SELECTORS = (
    ".notification-badge",
    ".msg-conversation-card__unread-count",
    ".msg-conversation-listitem__unread-count",
)


async def _load_more_conversations(page: Any, minimum: int, max_scrolls: int = 6) -> None:
    """Best-effort: LinkedIn lazy-loads older conversations as the list is scrolled, so the
    ~20 that render on first paint are all a plain query would ever see. Bounded and
    exception-safe throughout: a scroll that doesn't work just means fewer results
    back, never a tool failure.
    """
    for _ in range(max_scrolls):
        try:
            cards = await page.query_selector_all(_CARD_SELECTOR)
        except Exception:
            return
        if len(cards) >= minimum:
            return
        container = await page.query_selector(_LIST_SELECTOR)
        if container is None:
            return
        try:
            await container.evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
            await page.wait_for_timeout(500)
        except Exception:
            return


async def _extract_conversation_card(card: Any) -> dict | None:
    name = clean(await _first_text(card, _NAME_SELECTORS))
    preview = clean(await _first_text(card, _PREVIEW_SELECTORS))
    timestamp = await _first_text(card, _TIME_SELECTORS)

    conversation_id = None
    try:
        link = await card.query_selector("a[href*='/messaging/thread/']")
        if link is not None:
            conversation_id = _thread_id_from_href(await link.get_attribute("href"))
    except Exception:
        pass

    public_id_ = None
    try:
        profile_link = await card.query_selector("a[href*='/in/']")
        if profile_link is not None:
            public_id_ = _profile_id_from_href(await profile_link.get_attribute("href"))
    except Exception:
        pass

    unread = False
    try:
        class_attr = (await card.get_attribute("class")) or ""
        unread = "unread" in class_attr.lower()
        if not unread:
            unread = (await _query_first(card, _UNREAD_BADGE_SELECTORS)) is not None
    except Exception:
        pass

    if name is None and preview is None and conversation_id is None:
        return None  # this element matched a selector but not our expected shape: skip it

    return {
        "conversation_id": conversation_id,
        "participant_name": name,
        "participant_public_id": public_id_,
        "last_message_preview": fence(truncate(preview, _PREVIEW_LIMIT, "message preview"), "conversation.preview"),
        "timestamp": timestamp,
        "unread": unread,
    }


async def do_get_inbox(
    session: Session, queue: ActionQueue, limit: int = 20, unread_only: bool = False
) -> dict:
    async def _inner() -> dict:
        cap = max(1, limit)
        page = await session.goto("/messaging/", wait_for=_CONTAINER_SELECTOR)

        # unread_only filters after loading, so ask for headroom rather than the bare
        # minimum: otherwise a page full of read conversations returns nothing.
        await _load_more_conversations(page, minimum=cap * 3 if unread_only else cap)

        container = await page.query_selector(_CONTAINER_SELECTOR)
        if container is None:
            raise parse_failed("the messaging inbox")

        try:
            cards = await page.query_selector_all(_CARD_SELECTOR)
        except Exception:
            raise parse_failed("the conversation list")

        conversations: list[dict] = []
        for card in cards:
            convo = await _extract_conversation_card(card)
            if convo is None:
                continue
            if unread_only and not convo["unread"]:
                continue
            conversations.append(convo)
            if len(conversations) >= cap:
                break

        return {"conversations": conversations, "count": len(conversations)}

    return await _run_tool(queue, "get_inbox", _inner)


# ---------------------------------------------------------------------------
# Conversation thread
# ---------------------------------------------------------------------------

_THREAD_CONTAINER_SELECTOR = ".msg-s-message-list-container, .msg-s-message-list"
_MESSAGE_GROUP_SELECTOR = "li.msg-s-message-list__event, div.msg-s-message-list__event, li.msg-s-event-listitem"

_SENDER_SELECTORS = (
    ".msg-s-message-group__name",
    ".msg-s-event-listitem__name",
    "span.msg-s-message-group__profile-link",
)

_GROUP_TIME_SELECTORS = (
    "time.msg-s-message-group__timestamp",
    ".msg-s-event-listitem__timestamp",
    "time",
)

_BODY_SELECTORS = (
    ".msg-s-event-listitem__body",
    "p.msg-s-event-listitem__body",
    ".msg-s-event-listitem__message-bubble",
)


async def do_get_conversation(
    session: Session, queue: ActionQueue, conversation_id: str, limit: int = 50
) -> dict:
    async def _inner() -> dict:
        cid = _clean_conversation_id(conversation_id)

        page = await session.goto(
            f"/messaging/thread/{cid}/",
            wait_for=f"{_THREAD_CONTAINER_SELECTOR}, {_MESSAGE_GROUP_SELECTOR}",
        )
        # An unknown or expired thread id lands back on the inbox rather than 404ing:
        # the thread id missing from the landed URL is the only reliable signal.
        if f"/messaging/thread/{cid}" not in page.url:
            raise not_found("This conversation")

        try:
            groups = await page.query_selector_all(_MESSAGE_GROUP_SELECTOR)
        except Exception:
            raise parse_failed("the message thread")

        if not groups and (await page.query_selector(_THREAD_CONTAINER_SELECTOR)) is None:
            raise parse_failed("the message thread")

        messages: list[dict] = []
        for group in groups:
            sender = clean(await _first_text(group, _SENDER_SELECTORS))
            timestamp = await _first_text(group, _GROUP_TIME_SELECTORS)

            try:
                bodies = await group.query_selector_all(
                    ".msg-s-event-listitem__body, p.msg-s-event-listitem__body, "
                    ".msg-s-event-listitem__message-bubble"
                )
            except Exception:
                bodies = []

            if not bodies:
                own_body = clean(await _first_text(group, _BODY_SELECTORS))
                if own_body is not None:
                    messages.append(
                        {
                            "sender": sender,
                            "timestamp": timestamp,
                            "body": fence(truncate(own_body, _BODY_LIMIT, "message body"), "message.body"),
                        }
                    )
                continue

            for body_el in bodies:
                try:
                    raw = (await body_el.inner_text()).strip()
                except Exception:
                    continue
                text = clean(raw)
                if not text:
                    continue
                messages.append(
                    {
                        "sender": sender,
                        "timestamp": timestamp,
                        "body": fence(truncate(text, _BODY_LIMIT, "message body"), "message.body"),
                    }
                )

        # Best-effort only: LinkedIn lazy-loads older messages on scroll-up inside the
        # thread pane, and there is no reliable selector to wait on after a
        # programmatic scroll there, so very long threads may return fewer than
        # `limit` and only the most-recently-loaded messages are available.
        if limit and limit > 0:
            messages = messages[-limit:]

        return {"conversation_id": cid, "messages": messages, "count": len(messages)}

    return await _run_tool(queue, "get_conversation", _inner)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_SEARCH_BOX_SELECTORS = (
    "input[placeholder='Search messages']",
    "input[aria-label='Search messages']",
    ".msg-search-form input[type='text']",
    "input.msg-search-form__typeahead-input",
)


async def do_search_conversations(
    session: Session, queue: ActionQueue, query: str, limit: int = 20
) -> dict:
    async def _inner() -> dict:
        q = (query or "").strip()
        if not q:
            raise LinkedInError(
                "invalid_argument", "query is required.", "Pass a non-empty search term."
            )

        page = await session.goto("/messaging/", wait_for=_CONTAINER_SELECTOR)

        search_box = await _query_first(page, _SEARCH_BOX_SELECTORS)
        if search_box is None:
            raise parse_failed("the messaging search box")

        try:
            await search_box.click()
            await search_box.fill(q)
            # Results re-render asynchronously with no dedicated selector to wait on
            # (it's the same conversation-card markup, just filtered), a short fixed
            # wait for the debounce is the least brittle option here.
            await page.wait_for_timeout(800)
        except Exception:
            raise parse_failed("the messaging search results")

        try:
            cards = await page.query_selector_all(_CARD_SELECTOR)
        except Exception:
            raise parse_failed("the messaging search results")

        results: list[dict] = []
        for card in cards:
            if len(results) >= max(1, limit):
                break
            convo = await _extract_conversation_card(card)
            if convo is None:
                continue
            snippet = clean(await _first_text(card, _PREVIEW_SELECTORS))
            convo["matching_snippet"] = fence(
                truncate(snippet, _SNIPPET_LIMIT, "search snippet"), "conversation.search_snippet"
            )
            results.append(convo)

        return {"query": q, "results": results, "count": len(results)}

    return await _run_tool(queue, "search_conversations", _inner)


# ---------------------------------------------------------------------------
# Send message (write)
# ---------------------------------------------------------------------------

_COMPOSE_BOX_SELECTORS = (
    "div.msg-form__contenteditable[contenteditable='true']",
    "div[role='textbox'][contenteditable='true']",
    ".msg-form__contenteditable",
)

_SEND_BUTTON_SELECTORS = (
    "button.msg-form__send-button",
    "button[type='submit'].msg-form__send-button",
    "button[aria-label='Send']",
)

_MESSAGE_BUTTON_SELECTORS = (
    "button[aria-label^='Message ']",
    "a[aria-label^='Message ']",
    "button.pvs-profile-actions__action[aria-label^='Message']",
)


async def _reply_to_thread(session: Session, conversation_id: str, text: str) -> str:
    page = await session.goto(
        f"/messaging/thread/{conversation_id}/",
        wait_for=f"{_COMPOSE_BOX_SELECTORS[0]}, {_THREAD_CONTAINER_SELECTOR}",
    )
    if f"/messaging/thread/{conversation_id}" not in page.url:
        raise not_found("This conversation")

    box = await _query_first(page, _COMPOSE_BOX_SELECTORS)
    if box is None:
        raise parse_failed("the message compose box")

    try:
        await box.click()
        await box.fill(text)
    except Exception:
        raise parse_failed("the message compose box")

    send_btn = await _query_first(page, _SEND_BUTTON_SELECTORS)
    if send_btn is None:
        raise parse_failed("the send button")

    try:
        await send_btn.click()
    except Exception:
        raise LinkedInError(
            "send_failed",
            "Found the send button but clicking it did not appear to work.",
            "LinkedIn may have changed the compose flow. Check the conversation in your browser.",
        )

    # Same guarantee as the compose path: a click that did not throw is not
    # evidence of delivery.
    if not await _confirm_sent(page, text):
        raise LinkedInError(
            "send_unconfirmed",
            "The reply was attempted but never appeared in the thread.",
            "Treat this as NOT sent. Check the conversation in your browser before retrying, "
            "so you do not send it twice.",
        )

    return conversation_id


# LinkedIn's profile top card carries a "Message" control that is an <a>, not a
# button, and sits alongside decoys: "Message with Premium" upsells, and "Message
# <other person>" links in the sidebar. Matching on exact visible text inside the
# top card is what tells them apart; matching on class names does not work at all,
# since LinkedIn ships hashed per-build classes now.
_FIND_MESSAGE_CONTROL_JS = r"""() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const main = document.querySelector('main');
  if (!main) return null;
  const controls = [...main.querySelectorAll('a,button')];
  const exact = controls.find(c => clean(c.innerText).toLowerCase() === 'message');
  if (!exact) return null;
  exact.setAttribute('data-lnkdn-mcp-target', '1');
  return true;
}"""

# After clicking send, confirm the text actually landed in the thread. Without this
# the flow reports success for any click that merely did not throw, which is how a
# write tool ends up lying about having contacted a real person.
_MESSAGE_PRESENT_JS = r"""(needle) => {
  const body = (document.body.innerText || '').replace(/\s+/g, ' ');
  return body.includes(needle);
}"""


async def _confirm_sent(page: Any, text: str, attempts: int = 20) -> bool:
    """Poll until the sent text appears on the page, or give up.

    Deliberately checks the text rather than a toast or a spinner: a toast can
    appear for a draft, and a spinner tells you something is happening, not that
    it worked. The message being visible in the thread is the only signal that
    means what we want it to mean.
    """
    needle = " ".join(text.split())[:60]
    for _ in range(attempts):
        try:
            if await page.evaluate(_MESSAGE_PRESENT_JS, needle):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(400)
    return False


async def _message_new_recipient(session: Session, recipient: str, text: str) -> None:
    who = public_id(recipient)
    page = await session.goto(f"/in/{who}/", wait_for="main")

    # Structural first: mark the control whose visible text is exactly "Message",
    # then grab it. Falls back to the old selectors if LinkedIn serves an older
    # layout to this account.
    msg_btn = None
    try:
        if await page.evaluate(_FIND_MESSAGE_CONTROL_JS):
            msg_btn = await page.query_selector("[data-lnkdn-mcp-target]")
    except Exception:
        msg_btn = None
    if msg_btn is None:
        msg_btn = await _query_first(page, _MESSAGE_BUTTON_SELECTORS)
    if msg_btn is None:
        raise LinkedInError(
            "not_found",
            "No 'Message' button on this profile.",
            "LinkedIn only shows a Message button to 1st-degree connections (or via "
            "Premium InMail eligibility). Confirm you're connected to this person, or "
            "reply within an existing thread using conversation_id instead.",
        )

    try:
        await msg_btn.click()
    except Exception:
        raise parse_failed("the message compose overlay")

    box = None
    for _ in range(10):
        box = await _query_first(page, _COMPOSE_BOX_SELECTORS)
        if box is not None:
            break
        await page.wait_for_timeout(300)
    if box is None:
        raise parse_failed("the message compose overlay")

    try:
        await box.click()
        await box.fill(text)
    except Exception:
        raise parse_failed("the message compose box")

    send_btn = await _query_first(page, _SEND_BUTTON_SELECTORS)
    if send_btn is None:
        raise parse_failed("the send button")

    try:
        await send_btn.click()
    except Exception:
        raise LinkedInError(
            "send_failed",
            "Found the send button but clicking it did not appear to work.",
            "LinkedIn may have changed the compose flow. Check the conversation in your browser.",
        )

    # Prove it. Clicking without checking is how this tool previously reported
    # success for a message that was never delivered, which is worse than failing:
    # the caller believes they contacted someone and waits for a reply.
    if not await _confirm_sent(page, text):
        raise LinkedInError(
            "send_unconfirmed",
            "The send was attempted but the message never appeared in the conversation.",
            "Treat this as NOT sent. Open the conversation in your browser to check before "
            "trying again, so you do not send it twice. If the message is absent there too, "
            "LinkedIn has changed the compose flow and this is a bug worth reporting.",
        )


async def do_send_message(
    session: Session,
    queue: ActionQueue,
    conversation_id: str | None,
    recipient: str | None,
    message: str,
) -> dict:
    async def _inner() -> dict:
        text = (message or "").strip()
        if not text:
            raise LinkedInError(
                "invalid_argument", "The message is empty.", "Pass non-whitespace text to send."
            )
        if len(text) > MAX_MESSAGE_CHARS:
            raise LinkedInError(
                "invalid_argument",
                f"The message is {len(text)} characters, over the {MAX_MESSAGE_CHARS}-character "
                "limit this server enforces.",
                "Shorten the message and try again. It will not be sent truncated.",
            )

        cid = (conversation_id or "").strip() or None
        who = (recipient or "").strip() or None
        if bool(cid) == bool(who):
            raise LinkedInError(
                "invalid_argument",
                "Pass exactly one of conversation_id or recipient, not both or neither.",
                "Use conversation_id to reply in an existing thread, or recipient (a "
                "profile public id or URL) to start a new one.",
            )

        if cid:
            sent_cid = await _reply_to_thread(session, _clean_conversation_id(cid), text)
        else:
            assert who is not None
            await _message_new_recipient(session, who, text)
            sent_cid = None

        return {"sent": True, "conversation_id": sent_cid, "characters": len(text)}

    return await _run_tool(queue, "send_message", _inner)


# ---------------------------------------------------------------------------
# Connect (write)
# ---------------------------------------------------------------------------

_DEGREE_BADGE_SELECTORS = (
    "span.dist-value",
    ".pv-top-card--list-bullet li",
)

_CONNECT_BUTTON_SELECTORS = (
    "button[aria-label^='Invite'][aria-label*='to connect']",
    "button.pvs-profile-actions__action[aria-label*='Connect']",
    "button[aria-label='Connect']",
)

_PENDING_INDICATOR_SELECTORS = (
    "button[aria-label='Pending']",
    "button.artdeco-button--secondary[aria-label*='Pending']",
)

_MORE_ACTIONS_SELECTORS = (
    "button[aria-label='More actions']",
    "button.artdeco-dropdown__trigger[aria-label*='More']",
)

_MORE_MENU_CONNECT_SELECTORS = (
    "div[aria-label='Invite to connect']",
    "div[role='button'][aria-label*='to connect']",
)

_ADD_NOTE_BUTTON_SELECTORS = (
    "button[aria-label='Add a note']",
    "button:has-text('Add a note')",
)

_NOTE_TEXTAREA_SELECTORS = (
    "textarea#custom-message",
    "textarea[name='message']",
    "textarea.connect-button-send-invite__custom-message",
)

_SEND_INVITE_BUTTON_SELECTORS = (
    "button[aria-label='Send invitation']",
    "button[aria-label='Send without a note']",
    "button[aria-label='Send now']",
)

_WEEKLY_LIMIT_SELECTORS = (
    "div[role='alert']",
    ".artdeco-inline-feedback__message",
)


async def _connect_via_more_menu(page: Any) -> Any:
    """Several profile layouts tuck Connect inside the 'More actions' overflow menu
    instead of showing it as a primary button: this is the fallback path for those.
    """
    more_btn = await _query_first(page, _MORE_ACTIONS_SELECTORS)
    if more_btn is None:
        return None
    try:
        await more_btn.click()
        await page.wait_for_timeout(300)
    except Exception:
        return None
    return await _query_first(page, _MORE_MENU_CONNECT_SELECTORS)


async def do_connect(
    session: Session, queue: ActionQueue, profile: str, note: str | None = None
) -> dict:
    async def _inner() -> dict:
        who = public_id(profile)

        clean_note = (note or "").strip() or None
        if clean_note and len(clean_note) > MAX_NOTE_CHARS:
            raise LinkedInError(
                "invalid_argument",
                f"The note is {len(clean_note)} characters, over LinkedIn's "
                f"{MAX_NOTE_CHARS}-character cap for connection notes on free accounts.",
                "Shorten the note and try again. It will not be sent truncated.",
            )

        page = await session.goto(f"/in/{who}/", wait_for="main")

        # Degree badge is the cheapest "already connected" signal, when present.
        degree = await _first_text(page, _DEGREE_BADGE_SELECTORS)
        if degree and "1st" in degree:
            raise LinkedInError(
                "already_connected",
                "You are already connected to this person.",
                "No action taken: nothing to send.",
            )

        connect_btn = await _query_first(page, _CONNECT_BUTTON_SELECTORS)

        if connect_btn is None and (await _query_first(page, _PENDING_INDICATOR_SELECTORS)) is not None:
            raise LinkedInError(
                "already_pending",
                "A connection request to this person is already pending.",
                "Wait for them to respond, or withdraw the request from linkedin.com "
                "before sending a new one.",
            )

        if connect_btn is None and (await _query_first(page, _MESSAGE_BUTTON_SELECTORS)) is not None:
            # Degree badge selector may have missed; a Message button with no Connect
            # or Pending affordance is the fallback signal for already-connected.
            raise LinkedInError(
                "already_connected",
                "You are already connected to this person.",
                "No action taken: nothing to send.",
            )

        if connect_btn is None:
            connect_btn = await _connect_via_more_menu(page)

        if connect_btn is None:
            raise LinkedInError(
                "connect_unavailable",
                "No 'Connect' action is available on this profile.",
                "This usually means the profile is outside your network tier and only "
                "allows Follow or InMail, or LinkedIn has changed this profile's layout.",
            )

        try:
            await connect_btn.click()
        except Exception:
            raise parse_failed("the connect button")

        if clean_note:
            add_note_btn = await _query_first(page, _ADD_NOTE_BUTTON_SELECTORS)
            if add_note_btn is None:
                raise parse_failed("the 'add a note' option")
            try:
                await add_note_btn.click()
            except Exception:
                raise parse_failed("the 'add a note' option")

            note_box = await _query_first(page, _NOTE_TEXTAREA_SELECTORS)
            if note_box is None:
                raise parse_failed("the note field")
            try:
                await note_box.fill(clean_note)
            except Exception:
                raise parse_failed("the note field")

        send_btn = await _query_first(page, _SEND_INVITE_BUTTON_SELECTORS)
        if send_btn is None:
            # Some layouts send the invite the instant Connect is clicked, with no
            # modal at all. Only treat this as success if there's a concrete sign the
            # invite actually went out: otherwise it's a real parse failure.
            if (await _query_first(page, _PENDING_INDICATOR_SELECTORS)) is not None:
                return {"sent": True, "profile": who, "note_included": False}
            raise parse_failed("the send-invite button")

        try:
            await send_btn.click()
        except Exception:
            raise LinkedInError(
                "send_failed",
                "Found the send button but clicking it did not appear to work.",
                "LinkedIn may have changed the invite flow. Check the profile in your browser.",
            )

        await page.wait_for_timeout(500)
        limit_text = await _first_text(page, _WEEKLY_LIMIT_SELECTORS)
        if limit_text and "weekly" in limit_text.lower():
            raise LinkedInError(
                "invite_limit_reached",
                "LinkedIn's weekly invitation limit has been reached.",
                "This is LinkedIn's own cap, not this server's: it resets on a rolling "
                "weekly basis. Wait before sending more invitations.",
            )

        return {"sent": True, "profile": who, "note_included": clean_note is not None}

    return await _run_tool(queue, "connect", _inner)
