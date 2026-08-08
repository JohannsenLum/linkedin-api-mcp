<!-- mcp-name: io.github.JohannsenLum/linkedin-api-mcp -->

# linkedin-api-mcp


> **Disclaimer: This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.**

An MCP server for LinkedIn. It drives a real, headless browser
([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), an
undetected fork of Playwright) using **your own logged-in LinkedIn session cookie** —
there is no scraping API, no credential stuffing, no bypass of LinkedIn's login. Your
agent gets tools to read profiles, companies, jobs and posts, and — deliberately
separated out — two tools that take real actions on your behalf: sending a message
and sending a connection request.

Every action goes through a single browser session, one at a time, paced against
limits you control. See [Rate limits](#rate-limits) below.

## Install

Requires Python 3.11+. The server is published for [`uv`](https://docs.astral.sh/uv/),
so nothing needs a persistent virtualenv:

```bash
# One-time: install the Chromium build patchright drives.
uvx --from linkedin-api-mcp patchright install chromium

# One-time: store your session cookie in the OS keyring.
uvx linkedin-api-mcp auth

# Verify the cookie works before wiring it into a client.
uvx linkedin-api-mcp --test
```

`--test` prints the server's redacted configuration and confirms LinkedIn accepts
the stored session — it never prints the cookie itself.

### Getting your session cookie

LinkedIn doesn't offer an API key for this kind of access, so the server uses the
same session cookie your browser already holds:

1. Log in to [linkedin.com](https://www.linkedin.com) in a normal browser.
2. Open DevTools (`F12` or `Cmd+Opt+I`) → **Application** → **Cookies** →
   `https://www.linkedin.com`.
3. Find the row named `li_at` and copy its **Value**.
4. Run `uvx linkedin-api-mcp auth` and paste it in when prompted (the terminal will
   not echo it back).

> [!WARNING]
> **This cookie *is* your LinkedIn login.** Anyone who has it can act as you on
> LinkedIn — read your messages, message your connections, see everything your
> account can see — without needing your password, and it does not expire when you
> change one. Never paste it into a chat, a commit, an issue, a screenshot, or
> anywhere other than `linkedin-api-mcp auth`. If you ever suspect it has leaked,
> log out of that session from LinkedIn's **Settings → Sign in & security** page,
> which invalidates it immediately.

By default the cookie is stored in your OS keyring (Keychain, Windows Credential
Manager, or Secret Service on Linux), never written to a config file. For CI or a
throwaway environment, set `LINKEDIN_COOKIE` instead and skip `auth` entirely — the
environment variable takes priority when both are set.

## Tools

| Tool | Description |
|---|---|
| `get_profile` | Fetch a member's profile (headline, about, experience, education, skills) by URL or public identifier. |
| `get_my_profile` | Fetch the profile of the account this server is logged in as. |
| `search_people` | Search LinkedIn members by keyword. |
| `get_inbox` | List recent message thread previews. |
| `get_conversation` | Fetch the full message history of one thread, by id. |
| `search_conversations` | Search your message threads by participant or keyword. |
| `send_message` | **Write.** Sends a real message to another member from your account. |
| `connect` | **Write.** Sends a real connection invitation to another member from your account. |
| `get_company` | Fetch a company page (about, size, industry, recent posts). |
| `search_companies` | Search LinkedIn companies by keyword. |
| `search_jobs` | Search job postings by keyword and optional location. |
| `get_job` | Fetch one job posting in full. |
| `search_posts` | Search LinkedIn feed posts by keyword. |
| `linkedin_status` | Check session validity and current usage against the rate limits below. |

`send_message` and `connect` are the only two tools that change anything —
everything else only reads what your account can already see. Both are visible to
another real person the moment they run: there is no draft, preview, or undo step.

## Client configuration

All three clients spawn the same command over stdio; only the file differs.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add linkedin -- uvx linkedin-api-mcp
```

or add the same block as above to `.mcp.json`.

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

If you'd rather not rely on the keyring in a given client (e.g. a sandboxed
environment), pass the cookie directly instead of running `auth`:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"],
      "env": { "LINKEDIN_COOKIE": "your-li_at-value" }
    }
  }
}
```

Anywhere you do this, treat that config file with the same care as the cookie
itself — don't commit it, and restrict its permissions.

## FAQ

**Is this safe to use? Will I get banned?**

This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. LinkedIn's User Agreement prohibits automated access, and accounts using automated tools can be restricted or banned. Use at your own risk; there is no guarantee of account safety. If you encounter any issues, let me know in the Discussions.

**What if my agents execute too many actions?**

Tool calls run sequentially through a queue. You are responsible for the volume of automation you run; use it sparingly and prompt your agents responsibly.

## Rate limits

The queue answer above is enforced, not just claimed — every tool call, including
`linkedin_status`, passes through the same `ActionQueue` before it touches the
browser:

| Setting | Default | Env var | Effect |
|---|---|---|---|
| Minimum interval between actions | 2 seconds | `LINKEDIN_MIN_INTERVAL` | Calls are paced against the previous one, not run back-to-back. |
| Actions per rolling hour | 120 | `LINKEDIN_MAX_PER_HOUR` | Once hit, further calls fail immediately with a `rate_limited` error rather than queueing or sleeping. |

Both are read from the environment at startup. The ceiling is local to this server —
it exists to stop a looping agent from generating a burst of LinkedIn traffic, not
because LinkedIn told us these numbers. Lowering them is always safe; raising them
is you deciding you're willing to accept more risk than the defaults assume. Call
`linkedin_status` to see current usage against both before running a batch of
actions.

Other environment variables the server reads: `LINKEDIN_COOKIE` (overrides the
keyring), `LINKEDIN_HEADLESS` (default `true`), `LINKEDIN_NAV_TIMEOUT_MS` (default
`30000`).

## Licence

[MIT](LICENSE) © 2026 Johannsen Lum.

Use it, change it, redistribute it, build something commercial on it — the only
condition is that you keep the copyright notice and licence text. It comes with no
warranty of any kind.
