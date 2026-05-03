# Findings & Decisions

## Requirements
- User wants another Clawbot project, separate from the current social-sales bot.
- The new bot should collect information about tech, AI, and global government statistics.
- The bot should send a daily digest to the user's Telegram account.
- Delivery should run through a cron-triggered workflow.
- The project should live inside its own folder under `Clawbots`.

## Research Findings
- OpenClaw `2026.4.15` is installed locally and available on the command line.
- `C:\Users\admim1\.openclaw\openclaw.json` did not exist initially and has now been created with workspace, Telegram, and cron defaults.
- Python `3.12.0` is installed locally.
- `uv` is not installed locally, so the first scaffold uses plain Python packaging instead of an immediate `uv` workflow.
- OpenClaw config strings support `${ENV_VAR}` substitution, and Telegram also supports `TELEGRAM_BOT_TOKEN` env fallback for the default account. Sources: [Configuration Reference](https://docs.openclaw.ai/gateway/configuration-reference), [Telegram](https://docs.openclaw.ai/telegram)
- OpenClaw reads `.env` files from the current working directory and from `~/.openclaw/.env` without overriding existing process vars. Source: [Environment Variables](https://docs.openclaw.ai/help/environment)
- Running `openclaw onboard --non-interactive --mode local --auth-choice skip --install-daemon --skip-health --accept-risk` updated the global config and added gateway auth token metadata.
- Native Windows onboarding created Startup-folder fallback launcher files instead of a visible Scheduled Task:
  - `C:\Users\admim1\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\OpenClaw Gateway.cmd`
  - `C:\Users\admim1\.openclaw\gateway.cmd`
- `openclaw.json` now includes `gateway.auth.mode: "token"` and a generated local gateway token.
- OpenClaw state files exposed the pending Telegram peer details without needing a successful CLI pairing flow:
  - Telegram user/chat ID: `6275989697`
  - pairing code observed in local state during setup
- OpenClaw Telegram config has now been switched from `dmPolicy: "pairing"` to one-owner `dmPolicy: "allowlist"` with the discovered Telegram ID.
- `C:\Users\admim1\.openclaw\.env` now contains the Telegram bot token so the gateway can read it from its home directory.
- In this automation environment, the native Windows gateway process can be started repeatedly, but it still does not expose `127.0.0.1:18789` when launched from here.
- A direct Telegram Bot API send succeeded with chat ID `6275989697`, which proves the rotated token and destination chat are valid even though the OpenClaw gateway remains unstable in this shell.
- A project-local direct Telegram sender now works end-to-end through PowerShell wrappers and Python:
  - `scripts/send-telegram-direct.ps1`
  - `scripts/send-daily-digest.ps1`
- The current live digest pipeline uses:
  - Hacker News top stories for broad tech signals
  - OpenAI News RSS and Hugging Face Blog RSS for AI signals
  - optional Tavily search enrichment for tech and AI when `TAVILY_API_KEY` is configured
  - World Bank indicators for global stats snapshot
- Tavily's official Search API supports query-scoped web search with controls such as `topic`, `search_depth`, `time_range`, and `max_results`, which fits this bot's "official sources first, web enrichment second" design. Sources: [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search), [Tavily API Introduction](https://docs.tavily.com/documentation/api-reference/introduction)
- After the user added `TAVILY_API_KEY`, a live Tavily collector run returned 8 items and the digest selected at least one Tavily-sourced AI story into the top AI section.
- A live Telegram digest send with Tavily enabled succeeded with `message_id=14`.
- The project now stores sent-item history in SQLite at `state/daily_intel.db` so repeated runs can avoid resending the same top items for a few days.
- Windows Task Scheduler is now the active daily scheduler path:
  - task name: `Clawbot Daily Intel Digest`
  - schedule: daily at `08:00`
  - verified with a manual Scheduler-triggered run that finished with `Last Result: 0`
  - run logs are written to `logs/daily-digest-YYYY-MM-DD.log`
- Telegram's Bot API is an HTTP-based interface for building bots and is sufficient for a daily digest sender. Source: [Telegram Bot API](https://core.telegram.org/bots/api)
- The current user intent is Telegram only; the right control surface is fetch selection, not another delivery channel.
- A simple `.env`-based fetch-control layer is enough for this repo because the active collectors are fixed and named: `hacker_news`, `rss`, `tavily`, and `world_bank`.
- The current RSS feed names exposed for control are `OpenAI News` and `Hugging Face Blog`.
- The World Bank Indicators API exposes nearly 16,000 time series and does not require API keys for standard access. Source: [World Bank Indicators API](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation)
- OECD provides a free SDMX-based API, but it is rate-limited and should be queried responsibly. Source: [OECD Data API](https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html)
- FRED provides rich economic data APIs, but API-key handling is required. Source: [FRED API](https://fred.stlouisfed.org/docs/api/fred/)
- OpenAlex provides a modern research API and free daily budget with API-key support, making it a good choice for AI paper discovery. Source: [OpenAlex Docs](https://docs.openalex.org/)
- Hacker News exposes an official public API with near-real-time data and no documented rate limit in the official repository docs. Source: [Hacker News API](https://github.com/HackerNews/API)
- GDELT is useful as an optional broad-world monitoring source, but it is noisier than curated APIs and should not be the first source used for a clean MVP. Sources: [GDELT Project](https://www.gdeltproject.org/), [GDELT Data Access](https://www.gdeltproject.org/data.html)

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| Use a separate project folder `clawbot-daily-intel-telegram` | Keeps the multi-bot parent folder clean and scalable |
| Prioritize API/RSS ingestion over "crawl everything" | Official or quasi-official feeds are easier to keep stable |
| Make GDELT optional rather than core in MVP | It adds breadth, but also noise and complexity |
| Add dedupe and sent-history from day 1 | Daily digests become annoying quickly if duplicate items leak through |
| Separate collection, ranking, digesting, and sending into modules | This makes future topic expansion easier |
| Create the real OpenClaw global config now and keep the Telegram token in env, not JSON | Safer than hardcoding secrets into `openclaw.json` |
| Add local PowerShell helper scripts for gateway start and Telegram update checks | This reduces friction for the remaining manual steps |
| Keep the Telegram token in project-local `.env` and copy it to `~/.openclaw/.env` for gateway startup | Simpler and safer than hardcoding secrets into `openclaw.json`, and removes cwd ambiguity |
| Use Telegram allowlist mode instead of pairing for this bot | Better fit for a one-owner personal digest bot and avoids the stuck pairing CLI path here |
| Use standard-library HTTP/XML/SQLite for the MVP collectors | Avoids dependency-install friction and keeps the first digest pipeline runnable immediately on this machine |
| Use direct Telegram Bot API as the active outbound path | It is already verified and removes the flaky OpenClaw message-send dependency |
| Use Windows Task Scheduler in interactive mode for the first deployment | It works on this machine without extra services or WSL migration |
| Make Tavily optional and env-gated | The current digest already works, so Tavily should enrich rather than become a hard dependency |
| Add `.env`-based fetch controls for collectors, RSS feed selection, sections, and section sizes | The user wants direct control over what the daily Telegram run fetches and sends |

## Proposed MVP Source Mix
- Tech:
  - Hacker News API
  - curated RSS feeds from major engineering blogs
- AI:
  - OpenAlex API for research
  - curated lab/blog RSS feeds
- Global stats:
  - World Bank Indicators API
  - OECD SDMX API
  - FRED API
- Optional later:
  - GDELT
  - GitHub watchlists
  - custom site-specific scrapers

## Risks
- "Anything you can" is too broad without a source policy; this must be constrained.
- Telegram bot delivery is easy; source quality and ranking are the hard parts.
- Some sources are API-first, while others are RSS-first; a common normalized item schema is necessary.
- Government statistics often update on different cadences than daily news, so the digest must handle "no major update" cases gracefully.
- The Telegram token previously pasted in chat was compromised and had to be rotated before testing.
- Native Windows remains a deployment risk for OpenClaw; official docs still recommend WSL2 for the most reliable gateway setup.
- Telegram itself is no longer the blocker; the remaining blocker is the native Windows OpenClaw gateway launch path inside this automation environment.
- The current AI RSS mix is good enough for MVP, but source breadth is still narrow until OpenAlex, OECD, and optional FRED are added.
- Tavily is active now, but current ranking still favors strong Hacker News items on the tech side; ranking/source-balance tuning is the next improvement if more Tavily presence is desired.
- Because the scheduled task is `Interactive only`, the user session must be logged in for the task to run under the current configuration.
- `.env` values are string-based, but RSS feed selection is now matched case-insensitively to make feed control less brittle.

## Recommended First Build Order
1. Let the scheduler run for a few days and review digest quality
2. Add OpenAlex and OECD
3. Add optional FRED if API key is provided
4. Tune ranking and section balance after a few real digest runs
5. Add source allow/block lists
6. Add richer runtime alerting if needed

## Resources
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [World Bank Indicators API](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation)
- [OECD Data API](https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html)
- [FRED API](https://fred.stlouisfed.org/docs/api/fred/)
- [OpenAlex Docs](https://docs.openalex.org/)
- [Hacker News API](https://github.com/HackerNews/API)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily API Introduction](https://docs.tavily.com/documentation/api-reference/introduction)
- [GDELT Project](https://www.gdeltproject.org/)
- [GDELT Data Access](https://www.gdeltproject.org/data.html)

## Visual/Browser Findings
- No screenshots or PDFs were required for this planning pass.
- The most useful current-source findings are API capability, auth model, and rate-limit posture.

---
*Update this file after every 2 view/browser/search operations*
*This prevents research and source decisions from being lost*
