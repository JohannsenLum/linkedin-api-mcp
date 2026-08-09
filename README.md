<!-- mcp-name: io.github.JohannsenLum/linkedin-api-mcp -->

# linkedin-api-mcp

> **Disclaimer: This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.**

An MCP server for LinkedIn. It drives a real, headless browser
([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), an
undetected fork of Playwright) using **your own logged-in LinkedIn session cookie**.
There is no scraping API, no credential stuffing, no bypass of LinkedIn's login. Your
agent gets 12 read tools (profiles, companies, jobs, posts, your inbox) and two write
tools, kept deliberately separate: sending a message and sending a connection
request. Every action goes through a single browser session, one at a time, paced
against limits you control. See [Safety](#safety) below.

**Documentation: [mcp.johannsenlum.com/linkedin](https://mcp.johannsenlum.com/linkedin)**

[![PyPI](https://img.shields.io/pypi/v/linkedin-api-mcp)](https://pypi.org/project/linkedin-api-mcp/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/JohannsenLum/linkedin-api-mcp/blob/main/LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.JohannsenLum%2Flinkedin--api--mcp-1f6feb)](https://registry.modelcontextprotocol.io)
[![GitHub stars](https://img.shields.io/github/stars/JohannsenLum/linkedin-api-mcp?style=social)](https://github.com/JohannsenLum/linkedin-api-mcp)

<p align="center">
  <img src="https://raw.githubusercontent.com/JohannsenLum/linkedin-api-mcp/main/assets/header.svg"
       alt="Terminal demo of linkedin-api-mcp: an agent calls search_people to find a member, then send_message to message them, then reads the conversation back to confirm the message actually arrived. 14 tools, 68 tests, MIT licence."
       width="840">
</p>

## Install (one-click)

Deeplinks exist for Cursor and VS Code only; no other client has a documented
install-link format. These prefill the command below, nothing else needs filling
in since the cookie lives in your OS keyring, not an environment variable.

[![Add to Cursor](https://img.shields.io/badge/Cursor-Add_MCP_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=linkedin&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJsaW5rZWRpbi1hcGktbWNwIl19)
[![Add to VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=linkedin&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22linkedin-api-mcp%22%5D%7D)

### All clients

| Client | Deeplink? |
|---|---|
| [Claude Code](#config-claude-code) | no, one-line command |
| [Claude Desktop](#config-claude-desktop) | no |
| [Cursor](#config-cursor) | yes, above |
| [VS Code](#config-vscode) | yes, above |
| [Codex CLI](#config-codex) | no |
| [Zed](#config-zed) | no |
| [Windsurf](#config-windsurf) | no (Windsurf only resolves servers in its own registry) |

<a id="config-claude-code"></a>
<details>
<summary><strong>Claude Code</strong></summary>

```bash
claude mcp add linkedin -- uvx linkedin-api-mcp
```

</details>

<a id="config-claude-desktop"></a>
<details>
<summary><strong>Claude Desktop</strong>: <code>claude_desktop_config.json</code></summary>

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
Windows: `%APPDATA%\Claude\claude_desktop_config.json`

No one-click install exists for Claude Desktop (it installs `.mcpb` bundles, not
deeplinks). Copy this JSON in via **Settings → Developer → Edit Config**:

```jsonc
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

</details>

<a id="config-cursor"></a>
<details>
<summary><strong>Cursor</strong>: <code>~/.cursor/mcp.json</code></summary>

Fallback for the button above, or if you'd rather paste it directly:

```jsonc
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

</details>

<a id="config-vscode"></a>
<details>
<summary><strong>VS Code</strong>: <code>.vscode/mcp.json</code></summary>

Fallback for the button above, or if you'd rather paste it directly. Note VS
Code uses a `servers` key, not `mcpServers`:

```jsonc
{
  "servers": {
    "linkedin": {
      "type": "stdio",
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

</details>

<a id="config-codex"></a>
<details>
<summary><strong>Codex CLI</strong>: <code>~/.codex/config.toml</code></summary>

```toml
[mcp_servers.linkedin]
command = "uvx"
args = ["linkedin-api-mcp"]
```

Codex CLI's config schema has changed across versions and this block is not
independently verified against a live install. If it doesn't load, the reliable
path is running `uvx linkedin-api-mcp` yourself and pointing Codex at whatever
its current stdio-server config expects.

</details>

<a id="config-zed"></a>
<details>
<summary><strong>Zed</strong>: <code>settings.json</code></summary>

No deeplink exists for Zed. Add this under `context_servers` in your Zed
settings:

```jsonc
{
  "context_servers": {
    "linkedin": {
      "source": "custom",
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

</details>

<a id="config-windsurf"></a>
<details>
<summary><strong>Windsurf</strong>: <code>~/.codeium/windsurf/mcp_config.json</code></summary>

No deeplink exists for Windsurf. It only resolves servers from its own
registry, so this has to be pasted in manually via **Windsurf Settings → MCP
Servers → Edit raw config**:

```jsonc
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-api-mcp"]
    }
  }
}
```

</details>

If you'd rather not rely on the keyring in a given client (a sandboxed
environment, or CI), pass the cookie directly instead of running `auth`:

```jsonc
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
itself: don't commit it, and restrict its permissions.

## Getting your session cookie

LinkedIn doesn't offer an API key for this kind of access, so the server uses the
same session cookie your browser already holds.

1. One-time: install the Chromium build the server drives.
   ```bash
   uvx --from linkedin-api-mcp patchright install chromium
   ```
2. Log in to [linkedin.com](https://www.linkedin.com) in a normal browser.
3. Open DevTools (`F12` or `Cmd+Opt+I`) → **Application** → **Cookies** →
   `https://www.linkedin.com`.
4. Find the row named `li_at` and copy its **Value**.

> [!WARNING]
> **This cookie *is* your LinkedIn login.** Anyone who has it can act as you:
> read your messages, message your connections, see everything your account can
> see, without needing your password. It survives a password change, and it
> cannot be revoked from any session list LinkedIn shows you. Never paste it
> into a chat, a commit, an issue, a screenshot, or anywhere other than
> `linkedin-api-mcp auth`.

5. Store it:
   ```bash
   uvx linkedin-api-mcp auth
   ```
   Paste the value in when prompted; the terminal will not echo it back. By
   default it's stored in your OS keyring (Keychain, Windows Credential Manager,
   or Secret Service on Linux), never written to a config file.
6. Verify LinkedIn accepts it before wiring up a client:
   ```bash
   uvx linkedin-api-mcp --test
   ```
   Prints the server's redacted configuration and confirms the session is live.
   It never prints the cookie itself.

## Tools

| Tool | Description |
|---|---|
| `get_profile` | Fetch a member's profile (headline, about, experience, education, skills) by URL or public identifier. |
| `get_my_profile` | Fetch the profile of the account this server is logged in as. |
| `search_people` | Search LinkedIn members by keyword. |
| `get_inbox` | List recent message thread previews. |
| `get_conversation` | Fetch the full message history of one thread, by id. |
| `search_conversations` | Search your message threads by participant or keyword. |
| `send_message` ✏️ | **Write.** Sends a real message to another member from your account. |
| `connect` ✏️ | **Write.** Sends a real connection invitation to another member from your account. |
| `get_company` | Fetch a company page (about, size, industry, recent posts). |
| `search_companies` | Search LinkedIn companies by keyword. |
| `search_jobs` | Search job postings by keyword and optional location. |
| `get_job` | Fetch one job posting in full. |
| `search_posts` | Search LinkedIn feed posts by keyword. |
| `linkedin_status` | Check session validity and current usage against the rate limits in [Safety](#safety). |

14 tools: 12 read, 2 write. `send_message` and `connect` are the only two that
change anything, and both are visible to another real person the moment they
run: there is no draft, preview, or undo step. Everything else only reads what
your account can already see. Covered by 68 tests.

## What's verified, and what isn't

This is a days-old project. Rather than claim everything works, here's what's
actually been checked against a live account, and what hasn't.

| Status | Tool / behaviour | Note |
|---|---|---|
| Verified live | `get_my_profile`, `get_profile`, `search_people`, `get_company`, `search_companies`, `search_jobs`, `search_posts`, `get_inbox` | Called against a real account and returned real data. |
| Verified live | `send_message` ✏️ | A real message was sent, then the thread was read back to confirm it arrived. |
| Not yet verified | `connect` ✏️, `get_conversation`, `search_conversations`, `get_job`, `linkedin_status` | Implemented and tested, but not yet exercised against a live account by hand. |
| Known gap | `conversation_id` from `get_inbox` | Comes back `null`. LinkedIn binds inbox rows to in-memory JS objects rather than URLs, so there's no id to read out of the page. |
| Known gap | Reaction and comment counts on `search_posts` | Come back `null`. |
| Known gap | `experience` and `education` on `get_profile` | LinkedIn loads these sections only on scroll; they currently come back empty. |

## Safety

- **A serialised action queue.** Every tool call, including `linkedin_status`,
  passes through the same queue before it touches the browser: one action at a
  time, never in parallel.
- **A minimum interval and an hourly ceiling**, both enforced, not just claimed:

  | Setting | Default | Env var | Effect |
  |---|---|---|---|
  | Minimum interval between actions | 2 seconds | `LINKEDIN_MIN_INTERVAL` | Calls are paced against the previous one, not run back-to-back. |
  | Actions per rolling hour | 120 | `LINKEDIN_MAX_PER_HOUR` | Once hit, further calls fail immediately with a `rate_limited` error rather than queueing or sleeping. |

  The ceiling is local to this server: it exists to stop a looping agent from
  generating a burst of LinkedIn traffic, not because LinkedIn told us these
  numbers. Lowering them is always safe; raising them is you deciding you're
  willing to accept more risk than the defaults assume.
- **Prompt-injection fencing on all scraped free text.** Anything read off a
  LinkedIn page (a headline, an about section, a message) passes through your
  agent as data. It is fenced before your agent sees it, so text on a profile
  or in a message cannot pose as an instruction.
- **The cookie lives in your OS keyring**, not a config file, by default. See
  [Getting your session cookie](#getting-your-session-cookie).
- **`send_message` proves delivery before reporting success.** It polls the
  conversation until the sent text actually appears, and raises
  `send_unconfirmed` rather than a false `{"sent": true}` if it doesn't.

Other environment variables the server reads: `LINKEDIN_COOKIE` (overrides the
keyring), `LINKEDIN_HEADLESS` (default `true`), `LINKEDIN_NAV_TIMEOUT_MS` (default
`30000`).

## FAQ

**Is this safe to use? Will I get banned?**

This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. LinkedIn's User Agreement prohibits automated access, and accounts using automated tools can be restricted or banned. Use at your own risk; there is no guarantee of account safety. If you encounter any issues, let me know in the Discussions.

**What if my agents execute too many actions?**

Tool calls run sequentially through a queue. You are responsible for the volume of automation you run; use it sparingly and prompt your agents responsibly.

## Licence

[MIT](LICENSE) © 2026 Johannsen Lum.

Use it, change it, redistribute it, build something commercial on it: the only
condition is that you keep the copyright notice and licence text. It comes with no
warranty of any kind.

Contributions are accepted under the same licence.

## Contributing

Issues and pull requests are welcome:
[github.com/JohannsenLum/linkedin-api-mcp](https://github.com/JohannsenLum/linkedin-api-mcp).
Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

This is an independent project, not affiliated with LinkedIn Corporation. See the
disclaimer at the top of this document.
