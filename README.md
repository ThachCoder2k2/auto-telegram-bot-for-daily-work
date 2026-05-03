# Clawbot Daily Intel Telegram

This project sends a daily Telegram briefing for game/web development, hardware, gaming industry, global governance, and IELTS practice.

OpenClaw remains the scheduler/control layer. Actual Telegram delivery still uses the verified direct Bot API path inside this repo because `openclaw message send` is flaky on native Windows here.

## Current setup status

- OpenClaw installed: yes
- OpenClaw global config created: yes
- Project workspace configured for OpenClaw: yes
- Local `.env` file created: yes
- Telegram direct delivery verified: yes
- Windows scheduled Telegram task created: yes
- Native OpenClaw gateway listener from this automation shell: still unreliable on Windows
- Daily briefing mode is now `dev_ielts` by default

## What you can control

Edit `.env` to decide what the daily run fetches and how the briefing is framed:

- `BRIEFING_MODE`
  Default: `dev_ielts`; set `classic` to use the older tech/AI/stats digest
- `BRIEFING_LOCATION`
  Default: `Hanoi, Vietnam`
- `CURRENT_PROJECT_FOCUS`
  Default: `Godot horror-platformer`
- `PENDING_TASKS`
  Comma-separated tasks used by the Continuity Tracker
- `ENABLED_CATEGORIES`
  Values: `web_tech`, `hardware`, `gaming`, `governance`
- `NEWS_ITEMS_PER_CATEGORY`
  Default: `5`

- `ENABLED_COLLECTORS`
  Used by `classic` mode.
  Values: `hacker_news`, `rss`, `tavily`, `world_bank`
- `ENABLED_RSS_FEEDS`
  Used by `classic` mode.
  Current feed names: `OpenAI News`, `Hugging Face Blog`
- `ENABLED_SECTIONS`
  Used by `classic` mode.
  Values: `tech`, `ai`, `stats`, `watchlist`
- `HACKER_NEWS_LIMIT`
- `RSS_LIMIT_PER_FEED`
- `TECH_ITEM_LIMIT`
- `AI_ITEM_LIMIT`
- `WATCHLIST_ITEM_LIMIT`

Example:

```env
BRIEFING_MODE=dev_ielts
BRIEFING_LOCATION=Hanoi, Vietnam
CURRENT_PROJECT_FOCUS=Godot horror-platformer
PENDING_TASKS=Prototype one horror-platformer mechanic in Godot
ENABLED_CATEGORIES=web_tech,hardware,gaming,governance
NEWS_ITEMS_PER_CATEGORY=5
```

That generates the 5-module briefing: news table, inspiration lab, continuity tracker, IELTS section, and persistence tag.

## Local commands

- `scripts/show-fetch-config.ps1`: print the active fetch controls
- `scripts/print-daily-digest.ps1`: preview the live digest
- `scripts/send-daily-digest.ps1`: send the live Telegram digest
- `scripts/create-openclaw-cron.ps1`: add the default OpenClaw cron job

## Briefing structure

- Module 1: 5-5-5-5 news table across Web/Tech, Hardware, Gaming, and Governance
- Module 2: one Godot horror-platformer game idea and one web UI idea
- Module 3: continuity question plus next coding step
- Module 4: IELTS Band 8 structures, vocabulary, and speaking question
- Module 5: `[PERSISTENCE: ...]` tag for tomorrow

Telegram formatting uses plain-text blocks instead of markdown tables:
- a short header with date, location, and project focus
- separator lines between modules
- category blocks like `[WEB] Web/Tech`, `[GEAR] Hardware`, `[GAME] Gaming`, `[WORLD] Governance`
- top 3 readable headlines per category, with `+ N more scanned`
- top item in each category includes its article link
- numbered headlines with `[Impact/10 Label]`
- compact IELTS vocabulary bullets for mobile readability

## Source mix

- Tavily 24-hour web search for the new `dev_ielts` briefing
- Hacker News fallback for sparse game-dev/news categories
- Hacker News, RSS, Tavily, and World Bank remain available in `classic` mode

## Optional Tavily enrichment

The `dev_ielts` mode works best with `TAVILY_API_KEY` in `.env`; otherwise it falls back to Hacker News where possible.

Supported Tavily knobs:
- `TAVILY_API_KEY`
- `TAVILY_ENABLED=true|false`
- `TAVILY_MAX_RESULTS=4`
- `TAVILY_SEARCH_DEPTH=basic`
- `TAVILY_TOPIC=news`
- `TAVILY_TIME_RANGE=day`

## Optional OpenAI generation

When `OPENAI_ENABLED=true` and `OPENAI_API_KEY` is set, `dev_ielts` uses the OpenAI Responses API to generate the Inspiration Lab and IELTS sections from the visible daily news items. If the API call fails or is disabled, the bot falls back to the local rule-based generator.

Supported OpenAI knobs:
- `OPENAI_API_KEY`
- `OPENAI_ENABLED=true|false`
- `OPENAI_MODEL=gpt-5.4-mini`

## Persona rotation

When `BOT_PERSONA_ENABLED=true`, the bot rotates one persona per day and passes that persona into the AI-generated sections. Factual sections stay protected: headlines, links, scores, vocabulary meanings, and the persistence tag must remain accurate.

Supported persona knobs:
- `BOT_PERSONA_ENABLED=true|false`
- `BOT_PERSONA_ROTATION=daily`
- `BOT_PERSONA_POOL=succubus,milf_teacher,soft_girlfriend,rot_maiden,final_boss_queen`
- `BOT_PERSONA_FORCE=` to lock one persona for testing, for example `succubus`
- `BOT_PERSONA_SPICE_LEVEL=0..3`
- `BOT_PERSONA_SAFE_MODE=true|false`
- `BOT_PERSONA_IMAGE_ENABLED=true|false`
- `BOT_PERSONA_IMAGE_DIR=assets/personas`
- `BOT_PERSONA_IMAGE_URLS_PATH=assets/personas/image_urls.json`

Built-in personas:
- `succubus`: seductive servant, calls you `Master`
- `milf_teacher`: strict adult IELTS mentor, calls you `Student`
- `soft_girlfriend`: caring companion, calls you `babe`
- `rot_maiden`: solemn fantasy battle companion, calls you `my lord`
- `final_boss_queen`: arrogant productivity rival, calls you `little challenger`

Persona images:
- `scripts/generate-persona-cards.ps1` regenerates local portrait cards
- Add curated direct image URLs to `assets/personas/image_urls.json` if you want Pinterest-style art instead of the fallback cards
- On `--send-digest`, the bot sends the selected persona image before the text digest
- If image sending fails, the text digest still sends

For Pinterest-style images, use a direct image URL that Telegram can fetch. Normal Pinterest page links may fail; if they do, the bot falls back to the local generated card.

## Scheduler path

Windows Task Scheduler:
- `Clawbot Daily Intel Digest` at `08:00`

OpenClaw cron:
- use `scripts/create-openclaw-cron.ps1`

## Current next step

Tune `.env`, run `scripts/show-fetch-config.ps1`, then run `scripts/print-daily-digest.ps1` until the Telegram output looks right.
