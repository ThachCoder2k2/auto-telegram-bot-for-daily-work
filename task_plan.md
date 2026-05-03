# Task Plan: Clawbot Daily Intel Telegram

## Goal
Build a second Clawbot that collects useful daily information about tech, AI, and global government/economic statistics, ranks and summarizes the most important items, and sends one Telegram digest every day through a cron-triggered job.

## Current Phase
Phase 6

## Product Direction
- Project folder: `D:\My project\Automated Agent\Clawbots\clawbot-daily-intel-telegram`
- Delivery channel: personal Telegram account via bot message
- Trigger model: daily cron job in `Asia/Bangkok`
- Collection policy:
  - Tier 1: official APIs and RSS feeds first
  - Tier 2: public HTML pages only when robots.txt and site terms allow it
  - Tier 3: no login-gated, cookie-based, or anti-bot bypass collection

## Recommended Stack
- Language: Python
- Runtime: `uv` + Python 3.12
- HTTP: `httpx`
- Parsing: `feedparser`, `beautifulsoup4`, `lxml`
- Data models: `pydantic`
- Storage: SQLite first, Postgres later if needed
- Scheduler:
  - local/server cron for the real daily trigger
  - optional manual run CLI for testing
- Telegram delivery: Telegram Bot API over HTTPS
- Optional summarization:
  - rule-only MVP first
  - LLM summarization after dedupe/ranking is stable

## Phases

### Phase 1: Scope, Constraints, and Source Policy
- [x] Define the bot mission and delivery channel
- [x] Separate allowed API/RSS sources from risky scraping
- [x] Create project planning files in the new project folder
- [x] Create OpenClaw global config pointing to this workspace
- [x] Create local project scaffold and helper scripts
- [x] Finalize the exact daily sections in the digest
- **Status:** completed

### Phase 2: Source Inventory and Adapter Design
- [x] Create source adapters for `tech news`, `AI research`, `global statistics`, and optional `world signals`
- [x] Define per-source metadata fields: title, url, source, timestamp, topic, score, summary
- [x] Define collection windows for each source type
- [x] Add optional Tavily-based enrichment behind env config
- [ ] Define rate-limit and retry rules per source
- **Status:** in_progress

### Phase 3: Ranking, Deduplication, and Filtering
- [x] Add URL/content deduplication
- [x] Add topic classification: `tech`, `ai`, `stats`, `policy`, `markets`, `security`
- [x] Add ranking signals: recency, authority, novelty, cross-source confirmation
- [ ] Add allow/block lists for noisy or low-quality sources
- **Status:** in_progress

### Phase 4: Digest Generation
- [x] Define the Telegram digest format
- [x] Generate sectioned daily output with short summaries and source links
- [x] Add fallback formatting when there are too many items or too little signal
- [x] Add "top 5" and "watchlist" modes
- **Status:** completed

### Phase 5: Telegram Delivery
- [x] Create Telegram bot and capture `bot token`
- [x] Capture destination `chat_id` for the user's account/channel/group
- [x] Prepare `sendMessage` test path through OpenClaw CLI
- [x] Switch OpenClaw Telegram auth from `pairing` to one-owner `allowlist`
- [x] Add delivery logging and retry behavior
- **Status:** in_progress

### Phase 6: Cron and Operations
- [x] Create one manual run command for local testing
- [x] Create one daily cron entry for production
- [x] Add state tracking so the same item is not resent every day
- [x] Add runtime logs, health checks, and failure alerts
- **Status:** completed

### Phase 7: Verification and Rollout
- [x] Run dry-run digests without Telegram send
- [x] Run Telegram send to test chat
- [ ] Verify source freshness and digest quality for 3-7 days
- [ ] Tune ranking and section selection
- **Status:** pending

## Source Plan

### Tech / AI signal sources
- Hacker News API for high-signal tech stories
- Tavily Search API for optional web enrichment
- GitHub API for releases/trending repos/watch targets
- OpenAlex API for research discovery
- Official RSS feeds for selected labs, vendors, and engineering blogs

### Global statistics sources
- World Bank Indicators API
- OECD SDMX API
- FRED API

### Optional world monitoring sources
- GDELT for broad global news and policy signal detection

## Initial Digest Shape
1. Top Tech Signals
2. Top AI Signals
3. Global Stats Snapshot
4. Watchlist / Emerging Themes
5. Source Links

## Key Questions
1. Should the Telegram digest be in Vietnamese, English, or bilingual?
2. Should the bot send one combined digest or separate digests by topic?
3. Do you want only summaries, or also raw links grouped by topic?
4. Should this bot cover only public sources, or also your private watchlists later?
5. Do you want a local cron job on your machine or a server-side cron in deployment?

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Create this as a separate project folder | The parent `Clawbots` directory is now organized to hold multiple bots |
| Prefer official APIs and RSS feeds over arbitrary crawling | This reduces legal, reliability, and maintenance risk |
| Use Python first | The workload is crawler-heavy, scheduler-friendly, and benefits from Python's data tooling |
| Start with SQLite | Enough for dedupe, sent history, and daily state in an MVP |
| Keep Telegram delivery simple via `sendMessage` first | Fastest path to a daily working bot |
| Use native Python tooling first instead of `uv` | Python 3.12 is available locally, while `uv` is not installed yet |
| Keep the Telegram token out of config for now | The previously pasted token must be considered compromised and rotated first |
| Use OpenClaw's Windows Startup-folder fallback if Scheduled Tasks are unavailable | Native Windows onboarding created this fallback automatically |
| Use one-owner Telegram allowlist mode in OpenClaw | This avoids pairing flow issues and fits a personal digest bot |
| Store the Telegram token in `C:\Users\admim1\.openclaw\.env` as well as project `.env` | The gateway reliably looks for env in its home directory, while native Windows launch paths are inconsistent here |
| Use direct Telegram Bot API delivery from the project as the active path | `openclaw message send` was the unstable piece on native Windows, while direct Bot API delivery is already verified |
| Use Windows Task Scheduler as the daily runner | The machine timezone already matches Bangkok and the scheduled task path has been verified with `Last Result: 0` |
| Keep Tavily optional until a key is provided | Prevents the enrichment layer from blocking existing daily delivery |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| `uv` was not installed locally | 1 | Scaffolded the project with plain Python 3.12 first |
| OpenClaw config file did not exist yet | 1 | Created `C:\\Users\\admim1\\.openclaw\\openclaw.json` with safe defaults and no token |
| OpenClaw pairing CLI hangs in this automation environment | 2 | Bypassed pairing by switching Telegram to `dmPolicy: "allowlist"` with the discovered user ID |
| Native Windows gateway process still does not expose `127.0.0.1:18789` when launched from this automation environment | 5 | Escalate to a normal user-run PowerShell session or move the gateway to WSL2 if native Windows remains unstable |
| `openclaw message send` remained flaky even after inbound Telegram worked | 2 | Replaced the active delivery path with a direct Telegram Bot API sender inside the project |

## Notes
- The highest-risk phrase in the request is "anything you can." This should be interpreted as "anything allowed, stable, and maintainable," not unrestricted scraping.
- The real blocker for go-live will be source credentials and final scheduling environment, not Telegram itself.
- Telegram delivery itself is now proven with a direct Bot API send; the remaining blocker is only OpenClaw's native Windows gateway startup path in this automation shell.
- The current MVP already builds and sends a live digest from Hacker News, curated AI RSS feeds, and World Bank indicators.
- Tavily enrichment is now wired into the codebase and will activate automatically once `TAVILY_API_KEY` is present in `.env`.
- Re-read this file before choosing framework or deployment details.
