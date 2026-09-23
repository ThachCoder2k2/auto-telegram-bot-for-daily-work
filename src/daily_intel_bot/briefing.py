from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import escape
from html import unescape
import json
from pathlib import Path
import re
from urllib import parse, request
from zoneinfo import ZoneInfo

from daily_intel_bot.collectors import TAVILY_EXCLUDED_DOMAINS, collect_hacker_news
from daily_intel_bot.config import Settings
from daily_intel_bot.enrich import (
    EnrichmentRequest,
    build_take_context,
    parse_takes,
)
from daily_intel_bot.models import SignalItem
from daily_intel_bot.openai_client import (
    AIBriefingSections,
    generate_ai_briefing_sections,
    generate_item_takes,
)
from daily_intel_bot.gemini_client import (
    generate_ai_briefing_sections_gemini,
    generate_item_takes_gemini,
)
from daily_intel_bot.notion_tasks import (
    load_board_quietly,
    load_notes_quietly,
    remember_task_order,
)
from daily_intel_bot import health
from daily_intel_bot.obs import get_logger, is_transient_http_error, with_retries
from daily_intel_bot.persona import PersonaProfile, persona_intro, select_persona
from daily_intel_bot.reminders import (
    render_note_line,
    render_task_line,
    upcoming_notes,
)
from daily_intel_bot.state_store import StateStore, current_streak
from daily_intel_bot.tavily_client import TavilySearchSpec, search_tavily


_LOG = get_logger("briefing")


@dataclass(frozen=True, slots=True)
class BriefingCategorySpec:
    key: str
    label: str
    query: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefingNewsItem:
    category: str
    headline: str
    impact_score: int
    url: str
    source: str
    summary: str
    # Raw signals kept alongside the item so scoring stays inspectable rather
    # than being folded into impact_score at collection time.
    tavily_score: float = 0.0
    hn_points: int = 0
    hn_comments: int = 0
    published_at: datetime | None = None
    corroboration: int = 1
    why_it_matters: str = ""


@dataclass(slots=True)
class BriefingState:
    last_date: str | None
    tasks_remaining: list[str]
    current_project_focus: str
    streak: int
    blocked_reason: str
    next_micro_task: str


@dataclass(frozen=True, slots=True)
class FeedMemory:
    """Everything the ranker knows about past sends and user feedback.

    Gathered once per run so the collector does not re-query SQLite for every
    category.
    """

    recent_keys: set[str]
    mutes: list[tuple[str, str]]
    topic_feedback: dict[str, float]
    domain_counts: dict[str, int]
    focus_terms: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "FeedMemory":
        return cls(recent_keys=set(), mutes=[], topic_feedback={}, domain_counts={})

    def is_muted(self, title: str, url: str) -> bool:
        haystack = f"{title} {url}".lower()
        domain = _domain_of(url)
        for kind, value in self.mutes:
            if kind == "domain" and value and value in domain:
                return True
            if kind == "keyword" and value and value in haystack:
                return True
        return False


CATEGORY_SPECS: tuple[BriefingCategorySpec, ...] = (
    BriefingCategorySpec(
        key="web_tech",
        label="Web/Tech",
        query=(
            "last 24 hours React Godot AI developer tools DevOps software "
            "engineering news important for web and game developers in Hanoi"
        ),
        keywords=("react", "go", "ai", "agent", "devops", "developer"),
    ),
    BriefingCategorySpec(
        key="hardware",
        label="Hardware",
        query=(
            "last 24 hours MacBook M-series high-end PC laptop Hall Effect "
            "Rapid Trigger mechanical keyboard mobile tech news"
        ),
        keywords=("macbook", "laptop", "keyboard", "hall effect", "mobile", "gpu"),
    ),
    BriefingCategorySpec(
        key="gaming",
        label="Gaming",
        query=(
            "last 24 hours site:gamedeveloper.com OR site:gamesindustry.biz "
            "OR site:80.lv game development indie studio Godot horror platformer "
            "mechanics game industry news"
        ),
        keywords=(
            "game developer",
            "gamedeveloper",
            "game",
            "indie",
            "aaa",
            "story",
            "godot",
            "studio",
            "engine",
        ),
    ),
    BriefingCategorySpec(
        key="governance",
        label="Governance",
        query=(
            "last 24 hours global government technology regulation AI law "
            "digital economy policy Vietnam developer impact"
        ),
        keywords=("regulation", "law", "policy", "government", "ai", "economy"),
    ),
)

SOURCE_HINTS = (
    "gamedeveloper.com",
    "github.blog",
    "react.dev",
    "godotengine.org",
    "openai.com",
    "theverge.com",
    "arstechnica.com",
    "wired.com",
)

MAX_BRIEFING_CHARS = 12000
VISIBLE_NEWS_PER_CATEGORY = 3
USER_AGENT = "clawbot-daily-intel-telegram/0.1"

# Terms that make an item actually about building software.
WEB_TECH_TERMS = (
    "agent",
    "api",
    "browser",
    "chrome",
    "cli",
    "cloud",
    "code",
    "coding",
    "compiler",
    "css",
    "database",
    "debug",
    "deploy",
    "developer",
    "devops",
    "docker",
    "engine",
    "framework",
    "git",
    "github",
    "godot",
    "javascript",
    "kubernetes",
    "language",
    "library",
    "linux",
    "llm",
    "model",
    "open source",
    "postgres",
    "programming",
    "python",
    "react",
    "release",
    "runtime",
    "rust",
    "sdk",
    "server",
    "software",
    "sql",
    "typescript",
    "vercel",
    "webassembly",
    "wasm",
)

# Wire-service subjects that are not developer news on their own.
MARKET_NOISE_TERMS = (
    "bessent",
    "bond",
    "dow",
    "earnings",
    "election",
    "fed",
    "inflation",
    "investors",
    "nasdaq",
    "president",
    "rally",
    "s&p",
    "senate",
    "shares",
    "stocks",
    "tariff",
    "trump",
    "wall street",
)

REJECT_KEYWORDS = {
    "gaming": (
        "nba",
        "espn",
        "quarter highlights",
        "phoenix suns",
        "oklahoma city thunder",
        "orlando magic",
        "detroit pistons",
    ),
}

GAME_IDEAS = (
    "Fear-Synced Platforms: platforms only solidify while the player keeps a fear meter below a threshold; panic makes the level physically less reliable.",
    "Lantern Debt: every light source reveals safe paths but permanently teaches the monster the player's route, forcing tradeoffs between clarity and stealth.",
    "False Checkpoints: some save points are mimics; the player must inspect tiny environmental tells before trusting them.",
    "Audio Gravity: whispers pull the character sideways, turning sound design into a navigation hazard during platforming.",
)

WEB_IDEAS = (
    "Signal Cards: use stacked cards with tiny latency-aware micro-animations so dense news feels alive without becoming noisy.",
    "Spatial Briefing Board: arrange categories as zones on a lightweight map, letting the user scan impact before reading details.",
    "Command Palette Digest: expose filters like /ai, /hardware, /tasks so the Telegram brief can later become an interactive web dashboard.",
    "Ambient Progress UI: use subtle animated borders to show freshness, confidence, and urgency on each item.",
)


def build_daily_briefing(
    settings: Settings,
) -> tuple[str, list[SignalItem], tuple[tuple[str, str, str], ...]]:
    """Render today's brief.

    Returns the HTML text, the items worth recording as sent, and the
    vocabulary triples the caller should push into spaced repetition.
    """
    state = load_briefing_state(settings)
    store = StateStore(settings.state_db_path)
    # Memory the collector needs: what we already sent, and what the user muted.
    memory = FeedMemory(
        recent_keys=store.recent_sent_keys(within_days=settings.repeat_window_days),
        mutes=store.list_mutes(),
        topic_feedback=store.topic_feedback(),
        domain_counts=dict(store.domain_counts(within_days=7)),
        focus_terms=_focus_terms(state, settings),
    )
    categories = _enabled_category_specs(settings)
    news_by_category = {
        spec.key: _collect_category_news(settings, spec, memory)
        for spec in categories
    }
    selected_items = _to_signal_items(news_by_category)
    text, vocabulary = render_daily_briefing(
        settings,
        state,
        news_by_category,
        store,
    )
    return text, selected_items, vocabulary


def render_daily_briefing(
    settings: Settings,
    state: BriefingState,
    news_by_category: dict[str, list[BriefingNewsItem]],
    store: StateStore | None = None,
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    store = store or StateStore(settings.state_db_path)
    now = datetime.now(ZoneInfo(settings.timezone))
    tasks = state.tasks_remaining
    focus = state.current_project_focus or settings.current_project_focus
    first_task = tasks[0] if tasks else "Pick the next step — reply /task <text>"
    persona = select_persona(
        enabled=settings.bot_persona_enabled,
        rotation=settings.bot_persona_rotation,
        pool=settings.bot_persona_pool,
        forced_key=settings.bot_persona_force,
        now=now,
    )
    ai_sections = _generate_ai_sections(
        settings,
        news_by_category,
        focus,
        tasks,
        now,
        persona,
    )
    news_by_category = _apply_item_takes(settings, news_by_category, focus, tasks)
    # Streak is derived from the task-event log, not from a counter that only
    # ever got written back unchanged.
    streak = current_streak(store.done_days(today=now.date()), now.date())

    lines = [
        _briefing_title(persona),
        f"{now.strftime('%Y-%m-%d')} | {escape(settings.briefing_location)}",
        f"🎯 <b>Today's lens:</b> {escape(focus)}",
    ]
    if persona:
        lines.append(_persona_quote(persona, _briefing_intro(persona, now)))
    else:
        lines.append(escape(_briefing_intro(persona, now)))
    if persona and ai_sections:
        lines.extend(["", _persona_quote(persona, ai_sections.daily_note, with_name=True)])
    lines.extend([_rule(), "📰 <b>1. News Radar</b>"])
    if persona:
        lines.append(_persona_quote(persona, persona.news_line))
    top_pick = _top_pick(news_by_category)
    if top_pick:
        lines.append(
            f"⭐ <b>Top pick:</b> {_impact_bar(top_pick.impact_score)} · "
            f"<a href=\"{escape(top_pick.url)}\">{_shorten_html(top_pick.headline, 88)}</a>"
        )
    for spec in _enabled_category_specs(settings):
        category_items = news_by_category.get(spec.key, [])
        lines.extend(["", f"{_category_icon(spec.key)} <b>{escape(spec.label)}</b>"])
        if not category_items:
            lines.append("<i>No strong signal in the last 24h.</i>")
            continue
        display_items = [item for item in category_items if item.impact_score > 1]
        if display_items:
            lines.append(
                f"🎯 <i>{escape(_category_why_matters(spec.key, display_items, focus))}</i>"
            )
        for index, item in enumerate(display_items[:VISIBLE_NEWS_PER_CATEGORY], start=1):
            badges = _source_badge(item)
            if item.corroboration > 1:
                # Independent confirmation is worth surfacing, not just scoring.
                badges += f" <b>[×{item.corroboration} sources]</b>"
            lines.append(f"{index}. {_impact_bar(item.impact_score)} {badges}")
            lines.append(f"   <b>{_shorten_html(item.headline, 100)}</b>")
            if item.why_it_matters:
                lines.append(f"   💡 <i>{_shorten_html(item.why_it_matters, 170)}</i>")
            elif index == 1:
                summary = _clean_summary(item.summary)
                if summary:
                    lines.append(f"   <i>{_shorten_html(summary, 150)}</i>")
            lines.append(
                f"   🔗 <a href=\"{escape(item.url)}\">{escape(item.source)}</a>"
            )
        hidden_count = max(0, len(display_items) - VISIBLE_NEWS_PER_CATEGORY)
        if hidden_count:
            lines.append(f"   <i>+{hidden_count} more scanned</i>")

    if ai_sections:
        game_idea, web_idea = ai_sections.game_idea, ai_sections.web_idea
        game_feel = ai_sections.game_feel
        prototype_task = ai_sections.prototype_task
        web_use = ai_sections.web_use
    else:
        game_idea, web_idea = _inspiration_ideas(news_by_category, focus, now)
        game_feel = _game_feel(game_idea)
        prototype_task = _prototype_task(focus, game_idea)
        web_use = _web_use(web_idea)
    lines.extend([_rule(), "💡 <b>2. Inspiration Lab</b>"])
    if ai_sections:
        lines.append("🤖 <i>AI-assisted from today's sources.</i>")
    if persona:
        lines.append(_persona_quote(persona, persona.inspiration_line))
    if not ai_sections:
        # Only the rule-based fallback needs clamping; AI text is already
        # word-limited by the prompt, so clamping it just cut off payload.
        game_idea = _shorten(game_idea, 150)
        game_feel = _shorten(game_feel, 110)
        prototype_task = _shorten(prototype_task, 110)
        web_idea = _shorten(web_idea, 130)
        web_use = _shorten(web_use, 100)
    lines.append(f"🎮 <b>Mechanic:</b> {escape(game_idea)}")
    lines.append(f"😨 <b>How it feels:</b> {escape(game_feel)}")
    lines.append(f"🛠️ <b>Tiny prototype:</b> {escape(prototype_task)}")
    lines.append(f"🌐 <b>Web idea:</b> {escape(web_idea)}")
    lines.append(f"📌 <b>Use:</b> {escape(web_use)}")

    lines.extend([_rule(), "🧭 <b>3. Continuity Tracker</b>"])
    if persona:
        lines.append(_persona_quote(persona, persona.continuity_line))
    micro_task = state.next_micro_task or _next_project_step(focus, first_task)
    lines.append(f"🎯 <b>Pending:</b> {escape(first_task)}")
    if len(tasks) > 1:
        lines.append(f"📋 <b>Queued:</b> {len(tasks) - 1} more task(s)")
    lines.append(f"🧪 <b>Next micro-task:</b> {escape(micro_task)}")
    lines.append(f"🧱 <b>If blocked:</b> {escape(_blocked_prompt(state.blocked_reason))}")
    lines.append(f"🔥 <b>Streak:</b> {streak} day(s) {_streak_bar(streak)}")
    weekly = store.task_event_counts(within_days=7, today=now.date())
    if weekly:
        lines.append(
            "📈 <b>Last 7 days:</b> "
            f"{weekly.get('done', 0)} done · {weekly.get('blocked', 0)} blocked · "
            f"{weekly.get('dropped', 0)} dropped"
        )
    lines.extend(_render_notion_block(settings, store))
    lines.extend(_render_notes_block(settings, store, now))
    lines.append(
        "❓ <b>Reply:</b> <code>/done</code> · <code>/blocked why</code> · "
        "<code>/task new thing</code> · <code>/help</code>"
    )

    lines.extend([_rule(), "📚 <b>4. IELTS Mastery</b>"])
    if persona:
        lines.append(_persona_quote(persona, persona.ielts_line))
    if ai_sections:
        lines.extend(_render_ai_sentence_structures(ai_sections))
    else:
        lines.extend(_render_sentence_structures(news_by_category, focus))
    lines.extend(["", "🧩 <b>Academic vocabulary:</b>"])
    vocabulary = (
        _ai_academic_vocabulary(ai_sections)
        if ai_sections
        else _academic_vocabulary(news_by_category)
    )
    for word, meaning, example in vocabulary:
        lines.append(f"- <b>{escape(word)}</b>: {escape(meaning)}. {escape(example)}")

    # Spaced repetition: words learned on earlier days, surfaced when their
    # Leitner interval has elapsed. Without this the deck was write-only.
    if settings.vocab_review_enabled:
        due = store.due_vocabulary(limit=settings.vocab_review_limit, now=now)
        if due:
            lines.extend(["", "🔁 <b>Recall from earlier days:</b>"])
            for entry in due:
                lines.append(
                    f"- <b>{escape(entry.word)}</b> (box {entry.box}/5) → "
                    f"<tg-spoiler>{escape(entry.meaning)}</tg-spoiler>"
                )
            lines.append(
                "<i>Score them: <code>/got word</code> · <code>/missed word</code></i>"
            )

    lines.append("")
    lines.append("🎙️ <b>Teacher's Challenge:</b>")
    challenge = (
        ai_sections.teacher_challenge
        if ai_sections
        else _teacher_challenge(news_by_category, settings.briefing_location, focus)
    )
    lines.append(escape(challenge))
    answer_frame = (
        ai_sections.answer_frame
        if ai_sections
        else _answer_frame(news_by_category, focus)
    )
    band8_phrase = (
        ai_sections.band8_phrase
        if ai_sections
        else _band8_phrase(news_by_category)
    )
    lines.append(f"🧱 <b>Answer frame:</b> {escape(answer_frame)}")
    lines.append(f"✨ <b>Band 8 phrase:</b> {escape(band8_phrase)}")

    if settings.weekly_recap_enabled and now.weekday() == settings.weekly_recap_weekday:
        lines.extend(_render_weekly_recap(store, persona, streak, now.date()))

    task_text = "; ".join(tasks) if tasks else "None"
    lines.extend([_rule(), "💾 <b>5. State</b>"])
    if persona:
        lines.append(_persona_quote(persona, persona.closing_line))
    trend_line = _render_trend_line(store)
    if trend_line:
        lines.append(trend_line)
    scanned = sum(len(items) for items in news_by_category.values())
    configured = (
        "Gemini"
        if settings.ai_provider == "gemini" and settings.gemini_api_key
        else "OpenAI"
        if settings.openai_enabled and settings.openai_api_key
        else ""
    )
    # Report what happened, not what was configured. A bare "AI: local rules"
    # left no way to tell a disabled backend from a rate-limited one.
    if not configured:
        provider = "local rules"
    elif ai_sections is None:
        provider = f"local rules — {configured} unavailable"
    else:
        provider = configured
    lines.append(
        f"🤖 <i>{scanned} items scanned · AI: {escape(provider)} · "
        f"generated {now.strftime('%H:%M')} {escape(settings.timezone.split('/')[-1])}</i>"
    )
    if configured and settings.item_takes_enabled and not any(
        item.why_it_matters
        for items in news_by_category.values()
        for item in items
    ):
        lines.append("⚠️ <i>Per-item takes unavailable this run</i>")
    for problem in health.alerts(store, settings, now):
        lines.append(f"🚨 <i>{escape(problem)}</i>")
    lines.append(
        f"[PERSISTENCE: {now.strftime('%Y-%m-%d')} | Tasks Remaining: {escape(task_text)} | Current Project Focus: {escape(focus)}]"
    )
    return _trim_briefing("\n".join(lines).strip()), vocabulary


def _render_notion_block(settings: Settings, store: StateStore) -> list[str]:
    """The Notion board, shown beside the bot's own micro-task.

    Deliberately a separate block: the bot's task drives the streak, while
    Notion holds the real project backlog. Merging them would make /done
    ambiguous.
    """
    board = load_board_quietly(settings, store)
    if board is None or not board.tasks:
        return []
    shown = board.tasks[: settings.notion_task_limit]
    # Remember the numbering so /ndone <n> still resolves tomorrow.
    remember_task_order(store, shown)
    lines = ["", f"📥 <b>Notion — {len(board.tasks)} open</b>"]
    lines.extend(render_task_line(index, task) for index, task in enumerate(shown, 1))
    if len(board.tasks) > len(shown):
        lines.append(f"   <i>+{len(board.tasks) - len(shown)} more</i>")
    lines.append("   <i>Tick one off with <code>/ndone &lt;n&gt;</code></i>")
    return lines


def _render_notes_block(settings: Settings, store: StateStore, now: datetime) -> list[str]:
    """Dated entries, shown once a day in the brief.

    This is the 'occasionally' surface for things that are not chores: the
    brief already runs daily, so nothing extra has to be scheduled for a note
    that is still a week out.
    """
    entries = load_notes_quietly(settings, store)
    if not entries:
        return []
    upcoming = upcoming_notes(entries, now, settings.notion_notes_lookahead_days)
    if not upcoming:
        return []
    lines = ["", f"🗓️ <b>Coming up — next {settings.notion_notes_lookahead_days} days</b>"]
    lines.extend(render_note_line(entry, now) for entry in upcoming)
    return lines


def _streak_bar(streak: int) -> str:
    """Seven-day dot bar so the streak reads at a glance."""
    filled = max(0, min(7, streak))
    return "🔥" * filled + "·" * (7 - filled)


def _render_trend_line(store: StateStore) -> str:
    """One line naming what kept coming back this week."""
    terms = store.recurring_terms(within_days=7, min_hits=3, limit=3)
    if not terms:
        return ""
    listed = ", ".join(f"{term} ×{count}" for term, count in terms)
    return f"🔁 <i>Recurring this week: {escape(listed)}</i>"


def _render_weekly_recap(
    store: StateStore,
    persona: PersonaProfile | None,
    streak: int,
    today: date,
) -> list[str]:
    """Sunday-only section summarising the week from stored history."""
    lines = [_rule(), "🗓️ <b>Weekly Recap</b>"]
    if persona:
        lines.append(_persona_quote(persona, "Seven days, measured."))

    scanned = store.sent_count(within_days=7)
    topics = store.topic_counts(within_days=7)
    domains = store.domain_counts(within_days=7)
    tasks = store.task_event_counts(within_days=7, today=today)
    total_vocab, mastered = store.vocabulary_stats()

    lines.append(f"📰 <b>Items delivered:</b> {scanned}")
    if topics:
        listed = " · ".join(f"{topic} {count}" for topic, count in topics[:4])
        lines.append(f"🧭 <b>Category split:</b> {escape(listed)}")
    if domains:
        listed = " · ".join(f"{domain} {count}" for domain, count in domains[:3])
        lines.append(f"📡 <b>Loudest sources:</b> {escape(listed)}")
    terms = store.recurring_terms(within_days=7, min_hits=3, limit=5)
    if terms:
        listed = ", ".join(f"{term} ×{count}" for term, count in terms)
        lines.append(f"🔁 <b>Themes that persisted:</b> {escape(listed)}")

    lines.append(
        "✅ <b>Task velocity:</b> "
        f"{tasks.get('done', 0)} done · {tasks.get('blocked', 0)} blocked · "
        f"{tasks.get('dropped', 0)} dropped · streak {streak}d"
    )
    lines.append(f"🎓 <b>Vocabulary:</b> {total_vocab} learned · {mastered} mastered")

    recent = store.recent_task_events(limit=3)
    if recent:
        lines.append("🧾 <b>Latest entries:</b>")
        for event in recent:
            note = f" — {_shorten(event.note, 60)}" if event.note else ""
            lines.append(
                f"- {escape(event.day)} [{escape(event.status)}] "
                f"{escape(_shorten(event.task, 60))}{escape(note)}"
            )
    return lines


def _apply_item_takes(
    settings: Settings,
    news_by_category: dict[str, list[BriefingNewsItem]],
    focus: str,
    tasks: list[str],
) -> dict[str, list[BriefingNewsItem]]:
    """Attach an AI 'why it matters' line to the strongest items.

    Only the top few items are enriched: each one costs an article fetch, and
    the payoff drops fast below the fold.
    """
    if not settings.item_takes_enabled:
        return news_by_category

    use_gemini = settings.ai_provider == "gemini"
    provider = "gemini" if use_gemini else "openai"
    if use_gemini:
        if not settings.gemini_api_key:
            return news_by_category
    elif not settings.openai_enabled or not settings.openai_api_key:
        return news_by_category

    ranked = sorted(
        (
            item
            for items in news_by_category.values()
            for item in items[:VISIBLE_NEWS_PER_CATEGORY]
            if item.impact_score > 1
        ),
        key=lambda item: item.impact_score,
        reverse=True,
    )[: max(0, settings.item_takes_limit)]
    if not ranked:
        return news_by_category

    requests = [
        EnrichmentRequest(
            url=item.url,
            title=item.headline,
            summary=item.summary,
            category=item.category,
        )
        for item in ranked
    ]
    try:
        context = build_take_context(
            requests,
            focus,
            tasks,
            settings.briefing_location,
        )
        def _call() -> dict[str, object]:
            if use_gemini:
                return generate_item_takes_gemini(
                    settings.gemini_api_key,
                    settings.gemini_model,
                    context,
                )
            return generate_item_takes(
                settings.openai_api_key,
                settings.openai_model,
                context,
            )

        payload = with_retries(
            _call,
            attempts=2,
            backoff=3.0,
            logger=_LOG,
            label="item takes",
            should_retry=is_transient_http_error,
        )
        takes = parse_takes(payload, len(requests))
        health.record(StateStore(settings.state_db_path), provider)
    except Exception as exc:  # noqa: BLE001 - enrichment is strictly optional
        health.record(StateStore(settings.state_db_path), provider, exc)
        _LOG.warning(
            "item takes failed, rendering without them: %s: %s",
            type(exc).__name__,
            exc,
        )
        return news_by_category

    by_url = {requests[index].url: text for index, text in takes.items()}
    if not by_url:
        return news_by_category
    return {
        category: [
            replace(item, why_it_matters=by_url[item.url])
            if item.url in by_url
            else item
            for item in items
        ]
        for category, items in news_by_category.items()
    }


def load_briefing_state(settings: Settings) -> BriefingState:
    path = Path(settings.briefing_state_path)
    if not path.exists():
        return BriefingState(
            last_date=None,
            tasks_remaining=list(settings.pending_tasks),
            current_project_focus=settings.current_project_focus,
            streak=0,
            blocked_reason="",
            next_micro_task="",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return BriefingState(
            last_date=None,
            tasks_remaining=list(settings.pending_tasks),
            current_project_focus=settings.current_project_focus,
            streak=0,
            blocked_reason="",
            next_micro_task="",
        )
    tasks = data.get("tasksRemaining")
    if not isinstance(tasks, list):
        tasks = list(settings.pending_tasks)
    return BriefingState(
        last_date=str(data.get("lastDate") or "") or None,
        tasks_remaining=[str(task) for task in tasks if str(task).strip()],
        current_project_focus=str(
            data.get("currentProjectFocus") or settings.current_project_focus
        ),
        streak=_safe_int(data.get("streak"), default=0),
        blocked_reason=str(data.get("blockedReason") or ""),
        next_micro_task=str(data.get("nextMicroTask") or ""),
    )


def save_briefing_state(
    settings: Settings,
    state: BriefingState,
    last_date: str | None = None,
) -> None:
    """Write ``state`` back to disk, optionally stamping a new ``lastDate``."""
    payload = {
        "lastDate": last_date or state.last_date or "",
        # Written as-is: an empty list means "you finished everything", not
        # "reseed from PENDING_TASKS". The old fallback resurrected closed
        # tasks on the next write.
        "tasksRemaining": state.tasks_remaining,
        "currentProjectFocus": state.current_project_focus
        or settings.current_project_focus,
        "streak": state.streak,
        "blockedReason": state.blocked_reason,
        "nextMicroTask": state.next_micro_task,
    }
    path = Path(settings.briefing_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def persist_briefing_state(settings: Settings) -> None:
    now = datetime.now(ZoneInfo(settings.timezone))
    state = load_briefing_state(settings)
    save_briefing_state(settings, state, last_date=now.strftime("%Y-%m-%d"))


def _collect_category_news(
    settings: Settings,
    spec: BriefingCategorySpec,
    memory: FeedMemory | None = None,
) -> list[BriefingNewsItem]:
    """Gather, filter, score and rank one category.

    Tavily is the primary source with Hacker News as a widening fallback. Both
    paths now feed the same post-processing pass, so dedupe, mutes and
    multi-factor scoring apply no matter where an item came from.
    """
    memory = memory or FeedMemory.empty()
    candidates: list[BriefingNewsItem] = []

    if settings.tavily_enabled and settings.tavily_api_key:
        try:
            results = search_tavily(
                settings.tavily_api_key,
                TavilySearchSpec(
                    query=spec.query,
                    topic=settings.tavily_topic,
                    time_range=settings.tavily_time_range,
                    search_depth=settings.tavily_search_depth,
                    # Over-fetch: dedupe and mutes discard a chunk of this, and
                    # a thin pool is what used to force placeholder filler.
                    max_results=max(settings.news_items_per_category * 4, 10),
                    # Social and aggregator domains never carry the primary
                    # story, and HN is already covered by its own collector.
                    exclude_domains=TAVILY_EXCLUDED_DOMAINS
                    + tuple(
                        value for kind, value in memory.mutes if kind == "domain"
                    ),
                ),
            )
        except Exception as exc:
            _LOG.warning(
                "Tavily search failed for %s: %s: %s",
                spec.key,
                type(exc).__name__,
                exc,
            )
            results = []
        relevance = _normalized_tavily_scores(results)
        for position, result in enumerate(results):
            if not _is_category_relevant(
                spec,
                result.title,
                result.content,
                result.url,
            ):
                continue
            if _is_generic_url(result.url):
                continue
            candidates.append(
                BriefingNewsItem(
                    category=spec.key,
                    headline=result.title,
                    impact_score=0,  # assigned by _rank_category below
                    url=result.url,
                    source=_source_label(result.url),
                    summary=result.content,
                    tavily_score=relevance[position],
                    published_at=_parse_published(result.published_date),
                )
            )

    # Always top up from Hacker News. Previously this only ran when Tavily came
    # up short, which meant HN's engagement signal was invisible on good days.
    existing_urls = {item.url for item in candidates}
    for item in _hacker_news_fallback(settings, spec):
        if item.url in existing_urls:
            continue
        existing_urls.add(item.url)
        candidates.append(item)

    return _rank_category(settings, spec, candidates, memory)


def _rank_category(
    settings: Settings,
    spec: BriefingCategorySpec,
    candidates: list[BriefingNewsItem],
    memory: FeedMemory,
) -> list[BriefingNewsItem]:
    """Drop muted/duplicate items, score the rest, return the best first."""
    clusters = _cluster_stories(candidates)
    corroboration = _corroboration_counts(candidates, clusters)
    scored: list[BriefingNewsItem] = []
    seen_clusters: set[int] = set()

    for item, cluster in zip(candidates, clusters):
        if memory.is_muted(item.headline, item.url):
            continue
        key = _make_key(item.category, item.url, item.headline)
        if key in memory.recent_keys:
            # Already sent inside the repeat window; this is the dedupe that
            # dev_ielts mode never actually applied.
            continue
        if cluster in seen_clusters:
            # Same story from another outlet: it already counted towards
            # corroboration, so showing it again is just repetition.
            continue
        seen_clusters.add(cluster)
        hits = corroboration.get(cluster, 1)
        scored.append(
            BriefingNewsItem(
                category=item.category,
                headline=item.headline,
                impact_score=_impact_score(item, spec.keywords, memory, hits),
                url=item.url,
                source=item.source,
                summary=item.summary,
                tavily_score=item.tavily_score,
                published_at=item.published_at,
                hn_points=item.hn_points,
                hn_comments=item.hn_comments,
                corroboration=hits,
            )
        )

    scored.sort(key=lambda item: item.impact_score, reverse=True)
    return scored[: max(settings.news_items_per_category, VISIBLE_NEWS_PER_CATEGORY)]


def _cluster_stories(items: list[BriefingNewsItem]) -> list[int]:
    """Group items that tell the same story; returns a cluster id per item.

    Outlets rarely agree word for word — "Apple's M5 Ultra benchmarks leak"
    and "M5 Ultra Benchmarks Leak | Reuters" share most but not all of their
    terms — so exact fingerprints miss almost every real duplicate. Greedy
    Jaccard clustering over the meaningful tokens catches them instead.
    """
    cluster_tokens: list[frozenset[str]] = []
    assignments: list[int] = []
    for item in items:
        tokens = _story_tokens(item.headline)
        match = -1
        for index, existing in enumerate(cluster_tokens):
            if _jaccard(tokens, existing) >= STORY_MATCH_THRESHOLD:
                match = index
                break
        if match < 0:
            cluster_tokens.append(tokens)
            match = len(cluster_tokens) - 1
        assignments.append(match)
    return assignments


def _corroboration_counts(
    items: list[BriefingNewsItem],
    clusters: list[int],
) -> dict[int, int]:
    """How many distinct domains carry each clustered story.

    Two outlets running the same story is real signal; one aggregator
    repeating itself is not, so domains are counted uniquely.
    """
    domains: dict[int, set[str]] = {}
    for item, cluster in zip(items, clusters):
        domains.setdefault(cluster, set()).add(_domain_of(item.url))
    return {cluster: len(hosts) for cluster, hosts in domains.items()}


def _story_tokens(headline: str) -> frozenset[str]:
    """Meaningful words of a headline, with the outlet suffix removed."""
    stripped = re.split(r"\s+[-–—|]\s+", headline.strip())[0]
    return frozenset(
        word
        for word in _SIGNATURE_SPLIT.split(stripped.lower())
        if len(word) > 3 and word not in _SIGNATURE_STOPWORDS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _focus_terms(state: BriefingState, settings: Settings) -> tuple[str, ...]:
    """Meaningful words from the project focus, used as a relevance boost."""
    focus = state.current_project_focus or settings.current_project_focus
    pending = " ".join(state.tasks_remaining)
    words = _SIGNATURE_SPLIT.split(f"{focus} {pending}".lower())
    return tuple(
        {word for word in words if len(word) > 3 and word not in _SIGNATURE_STOPWORDS}
    )


def _hacker_news_fallback(
    settings: Settings,
    spec: BriefingCategorySpec,
) -> list[BriefingNewsItem]:
    fallback_items = []
    hn_items = collect_hacker_news(limit=max(settings.hacker_news_limit, 10))
    for item in hn_items:
        haystack = f"{item.title} {item.summary}".lower()
        if not _contains_any_term(haystack, spec.keywords):
            continue
        fallback_items.append(
            BriefingNewsItem(
                category=spec.key,
                headline=item.title,
                impact_score=0,
                url=item.url,
                source=item.source,
                summary=item.summary,
                hn_points=_safe_int(item.metadata.get("score")),
                hn_comments=_safe_int(item.metadata.get("comments")),
                published_at=item.published_at,
            )
        )

    if spec.key == "gaming":
        # Generic HN rarely carries game-dev stories; widen with a targeted
        # search rather than padding the section with placeholders.
        used_urls = {item.url for item in fallback_items}
        fallback_items.extend(
            _hacker_news_search_fallback(settings, spec, used_urls)
        )
    return fallback_items


def _hacker_news_search_fallback(
    settings: Settings,
    spec: BriefingCategorySpec,
    used_urls: set[str],
) -> list[BriefingNewsItem]:
    queries = (
        "godot game development",
        "indie game development",
        "horror game platformer",
    )
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    hits: list[BriefingNewsItem] = []
    for query in queries:
        params = parse.urlencode(
            {
                "query": query,
                "tags": "story",
                "hitsPerPage": settings.news_items_per_category,
                "numericFilters": f"created_at_i>{cutoff}",
            }
        )
        url = f"https://hn.algolia.com/api/v1/search_by_date?{params}"
        req = request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
        for hit in payload.get("hits", []):
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or "").strip()
            story_url = str(hit.get("url") or "").strip()
            if not title or not story_url or story_url in used_urls:
                continue
            if _is_generic_url(story_url):
                continue
            haystack = f"{title} {story_url}".lower()
            if not any(keyword in haystack for keyword in spec.keywords):
                continue
            used_urls.add(story_url)
            created = hit.get("created_at_i")
            hits.append(
                BriefingNewsItem(
                    category=spec.key,
                    headline=title,
                    impact_score=0,
                    url=story_url,
                    source="Hacker News",
                    summary=str(hit.get("story_text") or "").strip(),
                    hn_points=_safe_int(hit.get("points")),
                    hn_comments=_safe_int(hit.get("num_comments")),
                    published_at=(
                        datetime.fromtimestamp(int(created), tz=timezone.utc)
                        if isinstance(created, (int, float))
                        else None
                    ),
                )
            )
    return hits


def _enabled_category_specs(settings: Settings) -> list[BriefingCategorySpec]:
    enabled = {value.strip().lower() for value in settings.enabled_categories}
    return [spec for spec in CATEGORY_SPECS if spec.key in enabled]


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _impact_score(
    item: BriefingNewsItem,
    keywords: tuple[str, ...],
    memory: FeedMemory,
    corroboration: int,
) -> int:
    """Blend every available signal into a 1-10 score.

    The old version only had Tavily's relevance plus two flat bonuses, so in
    practice it emitted 5 or 7 and nothing else. These factors are additive on
    a float scale and only rounded at the end, which keeps the spread visible.
    """
    haystack = f"{item.headline} {item.summary}".lower()
    title_url = f"{item.headline} {item.url}".lower()
    score = 3.0

    # Search relevance, when the item came from Tavily.
    score += max(0.0, min(item.tavily_score, 1.0)) * 3.0

    # Community engagement, when it came from Hacker News. Log-ish curve so a
    # 900-point story does not swamp everything else.
    if item.hn_points:
        score += min(2.5, item.hn_points / 150.0)
    if item.hn_comments:
        score += min(1.0, item.hn_comments / 120.0)

    # Freshness: full credit under 6h, fading to zero at 48h.
    if item.published_at is not None:
        age_hours = max(
            0.0,
            (datetime.now(timezone.utc) - item.published_at).total_seconds() / 3600.0,
        )
        score += max(0.0, min(1.5, (48.0 - age_hours) / 28.0))

    if _contains_any_term(haystack, keywords):
        score += 0.8
    if any(hint in title_url for hint in SOURCE_HINTS):
        score += 0.8

    # Relevance to what the user is actually building right now.
    if memory.focus_terms and _contains_any_term(title_url, memory.focus_terms):
        score += 1.5

    # Independent outlets carrying the same story.
    if corroboration > 1:
        score += min(1.5, (corroboration - 1) * 0.75)

    # Learned preference from /more and /less.
    score += memory.topic_feedback.get(item.category, 0.0)

    # Source fatigue: a domain that already dominated the last week gets
    # damped so one outlet cannot own the brief.
    domain_hits = memory.domain_counts.get(_domain_of(item.url), 0)
    if domain_hits >= 5:
        score -= 1.5
    elif domain_hits >= 3:
        score -= 0.7

    return max(1, min(10, round(score)))


def _is_category_relevant(
    spec: BriefingCategorySpec,
    title: str,
    content: str,
    url: str,
) -> bool:
    haystack = f"{title} {content} {url}".lower()
    title_url = f"{title} {url}".lower()
    if any(keyword in haystack for keyword in REJECT_KEYWORDS.get(spec.key, ())):
        return False
    if spec.key == "web_tech":
        # This branch used to fall through to "return True", so market and
        # politics wire copy landed in the developer section.
        if _contains_any_term(title_url, MARKET_NOISE_TERMS) and not _contains_any_term(
            title_url, WEB_TECH_TERMS
        ):
            return False
        return _contains_any_term(title_url, WEB_TECH_TERMS)
    if spec.key == "hardware":
        hardware_terms = spec.keywords + (
            "m-series",
            "m4",
            "m5",
            "nvidia",
            "amd",
            "intel",
            "snapdragon",
            "wooting",
            "keychron",
            "rapid trigger",
        )
        return _contains_any_term(title_url, hardware_terms)
    if spec.key == "gaming":
        return any(keyword in haystack for keyword in spec.keywords)
    if spec.key == "governance":
        policy_terms = (
            "ai act",
            "antitrust",
            "ban",
            "bill",
            "court",
            "data act",
            "digital markets act",
            "economy",
            "ftc",
            "government",
            "lawmakers",
            "minister",
            "policy",
            "regulation",
            "regulator",
            "tariff",
            "trade",
            "vietnam",
        )
        tech_terms = (
            "ai",
            "cyber",
            "data",
            "digital",
            "platform",
            "privacy",
            "semiconductor",
            "technology",
            "tech",
        )
        return _contains_any_term(title_url, policy_terms) and _contains_any_term(
            title_url, tech_terms
        )
    return True


def _is_generic_url(url: str) -> bool:
    match = re.match(r"https?://(?:www\.)?([^/]+)(/.*)?$", url.strip())
    if not match:
        return True
    path = (match.group(2) or "").strip("/")
    if not path:
        return True
    generic_paths = {"news", "blog", "articles", "latest", "business", "technology"}
    return path.lower() in generic_paths


_SIGNATURE_SPLIT = re.compile(r"[^a-z0-9]+")

# Token overlap above which two headlines are treated as the same story.
STORY_MATCH_THRESHOLD = 0.5

# Words too common in headlines to identify a story.
_SIGNATURE_STOPWORDS = frozenset(
    {
        "after",
        "again",
        "amid",
        "announce",
        "announces",
        "best",
        "could",
        "first",
        "from",
        "have",
        "here",
        "into",
        "just",
        "latest",
        "more",
        "most",
        "must",
        "news",
        "next",
        "over",
        "report",
        "says",
        "than",
        "that",
        "their",
        "them",
        "this",
        "under",
        "what",
        "when",
        "will",
        "with",
        "would",
        "your",
    }
)


def _normalized_tavily_scores(results: list) -> list[float]:
    """Rescale Tavily relevance to 0-1 within a single response.

    Tavily returns absolute scores around 0.01-0.05, so the old
    ``min(score, 1.0) * 4`` term evaluated to zero for every item and the
    relevance signal was silently discarded. Only the ordering inside one
    response is meaningful, so normalise against that response's own range.
    """
    scores = [max(0.0, float(getattr(result, "score", 0.0))) for result in results]
    if not scores:
        return []
    low, high = min(scores), max(scores)
    if high - low < 1e-9:
        # A flat response carries no ordering information; treat it as neutral
        # rather than crowning an arbitrary winner.
        return [0.5] * len(scores)
    return [(score - low) / (high - low) for score in scores]


def _parse_published(value: str | None) -> datetime | None:
    """Parse Tavily's published_date, which arrives as RFC 2822 or ISO 8601."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _domain_of(url: str) -> str:
    try:
        return parse.urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _contains_any_term(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        term = term.strip().lower()
        if not term:
            continue
        if " " in term or "-" in term:
            if term in text:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            return True
    return False


def _to_signal_items(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> list[SignalItem]:
    items: list[SignalItem] = []
    for category_items in news_by_category.values():
        for item in category_items:
            items.append(
                SignalItem(
                    key=_make_key(item.category, item.url, item.headline),
                    topic=item.category,
                    source=item.source,
                    title=item.headline,
                    url=item.url,
                    published_at=None,
                    score_hint=float(item.impact_score),
                    summary=item.summary[:180],
                )
            )
    return items


def _generate_ai_sections(
    settings: Settings,
    news_by_category: dict[str, list[BriefingNewsItem]],
    focus: str,
    tasks: list[str],
    now: datetime,
    persona: PersonaProfile | None,
) -> AIBriefingSections | None:
    use_gemini = settings.ai_provider == "gemini"
    if use_gemini:
        if not settings.gemini_api_key:
            return None
    elif not settings.openai_enabled or not settings.openai_api_key:
        return None
    context = {
        "date": now.strftime("%Y-%m-%d"),
        "location": settings.briefing_location,
        "current_project_focus": focus,
        "pending_tasks": tasks,
        "news_items": _ai_news_context(news_by_category),
        "output_rules": {
            "tone": "concise, useful, not dry",
            "game_context": "Godot horror-platformer",
            "web_context": "Telegram digest that may become a web dashboard",
            "ielts_goal": "Band 8.0+",
            "vocabulary_examples": "game-dev context",
            "inspiration_shape": "mechanic, game feel, tiny prototype task, web idea, workflow use",
            "speaking_shape": "question, answer frame, one Band 8 phrase",
        },
        "persona": _persona_context(settings, persona),
    }
    provider = "gemini" if use_gemini else "openai"

    def _call() -> AIBriefingSections:
        if use_gemini:
            return generate_ai_briefing_sections_gemini(
                settings.gemini_api_key,
                settings.gemini_model,
                context,
            )
        return generate_ai_briefing_sections(
            settings.openai_api_key,
            settings.openai_model,
            context,
        )

    try:
        # Retry transient faults before giving up: a single 503 used to drop
        # the whole brief to the rule-based fallback for the day.
        sections = with_retries(
            _call,
            attempts=3,
            backoff=3.0,
            logger=_LOG,
            label=f"AI sections via {provider}",
            should_retry=is_transient_http_error,
        )
        health.record(StateStore(settings.state_db_path), provider)
    except Exception as exc:
        health.record(StateStore(settings.state_db_path), provider, exc)
        _LOG.warning(
            "AI sections via %s failed, using rule-based fallback: %s: %s",
            provider,
            type(exc).__name__,
            exc,
        )
        return None
    if len(sections.sentence_structures) < 3 or len(sections.vocabulary) < 5:
        return None
    return sections


def _ai_news_context(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> list[dict[str, object]]:
    context_items: list[dict[str, object]] = []
    for category, category_items in news_by_category.items():
        visible_items = [
            item for item in category_items if item.impact_score > 1
        ][:VISIBLE_NEWS_PER_CATEGORY]
        for item in visible_items:
            context_items.append(
                {
                    "category": category,
                    "headline": _clean_headline(item.headline),
                    "impact_score": item.impact_score,
                    "source": item.source,
                    "summary": _shorten(item.summary, 220),
                }
            )
    return context_items


def _persona_context(
    settings: Settings,
    persona: PersonaProfile | None,
) -> dict[str, object]:
    if persona is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "key": persona.key,
        "name": persona.name,
        "address": persona.address,
        "role": persona.role,
        "voice": persona.voice,
        "spice_level": settings.bot_persona_spice_level,
        "safe_mode": settings.bot_persona_safe_mode,
        "rules": (
            "Write daily_note as natural direct conversation, like this persona is "
            "checking in with the user before the briefing. Apply persona flavor to "
            "game_idea, web_idea, sentence structures, "
            "vocabulary examples, and teacher_challenge. Call the user by the "
            "persona address when natural. Keep the briefing useful. Do not add "
            "explicit sexual acts, graphic anatomy, coercion, or minor-coded "
            "school language. Do not alter facts or cite unseen news."
        ),
    }


def _render_ai_sentence_structures(sections: AIBriefingSections) -> list[str]:
    lines: list[str] = []
    for structure in sections.sentence_structures[:3]:
        label = _shorten(structure.label, 28)
        sentence = _shorten(structure.sentence, 260)
        lines.append(f"- <b>{escape(label)}:</b> {escape(sentence)}")
    return lines


def _ai_academic_vocabulary(
    sections: AIBriefingSections,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            _shorten(entry.word, 28),
            _shorten(entry.vietnamese_meaning, 42),
            _shorten(entry.example, 160),
        )
        for entry in sections.vocabulary[:5]
    )


def _category_why_matters(
    category_key: str,
    items: list[BriefingNewsItem],
    focus: str,
) -> str:
    return {
        "web_tech": "Prototype speed, tool choice, and deployment risk.",
        "hardware": "Budget, input latency, and test-device choices.",
        "gaming": "Market pressure and mechanic inspiration.",
        "governance": "Rules, data, payments, and cross-border work.",
    }.get(category_key, "Daily planning signal.")


def _source_badge(item: BriefingNewsItem) -> str:
    quality = _source_quality(item)
    return f"<b>[{escape(quality)}]</b>"


def _source_quality(item: BriefingNewsItem) -> str:
    source = item.source.lower()
    url = item.url.lower()
    if "hacker news" in source or "news.ycombinator" in url:
        return "HN"
    if any(domain in source for domain in ("react.dev", "godotengine.org", "github.blog", "openai.com")):
        return "Official"
    if any(domain in source for domain in ("gamedeveloper", "gamesindustry", "80.lv")):
        return "Industry"
    if any(domain in source for domain in ("techcrunch", "theverge", "wired", "arstechnica", "gizmodo", "cnet")):
        return "Press"
    if any(domain in source for domain in ("law", "gov", "europa", "oecd", "worldbank")):
        return "Policy"
    return "Watch"


def _game_feel(game_idea: str) -> str:
    text = game_idea.lower()
    if any(word in text for word in ("echo", "audio", "whisper", "sound")):
        return "Paranoid and readable: the player hears danger before they fully understand it."
    if any(word in text for word in ("light", "lantern", "shadow")):
        return "Unsafe clarity: every reveal helps the player and exposes them."
    if any(word in text for word in ("checkpoint", "mimic", "false")):
        return "Distrustful but fair: the player learns to inspect before committing."
    return "Tense but testable: one simple rule creates pressure without confusing the player."


def _prototype_task(focus: str, game_idea: str) -> str:
    if "godot" in focus.lower():
        mechanic = _shorten(game_idea.split(":")[0], 28)
        return f"Build one room with {mechanic}, one hazard, one safe exit, and a 60-second test."
    return "Make a 45-minute prototype that proves one mechanic before adding polish."


def _web_use(web_idea: str) -> str:
    text = web_idea.lower()
    if "filter" in text or "palette" in text:
        return "Lets you jump from daily reading to action without scrolling through noise."
    if "card" in text or "digest" in text:
        return "Turns the Telegram brief into a dashboard you can scan in under a minute."
    return "Makes daily intel easier to sort by urgency, project impact, and follow-up tasks."


def _blocked_prompt(blocked_reason: str) -> str:
    if blocked_reason.strip():
        return f"Current blocker: {blocked_reason.strip()}"
    return "If Godot, scope, or assets block you, write the blocker in one sentence."


def _briefing_title(persona: PersonaProfile | None) -> str:
    if persona is None:
        return "🧠 <b>Daily Intel for Andru</b>"
    return (
        f"{escape(persona.icon)} <b>Daily Intel for Andru</b> "
        f"<i>with {escape(persona.name)}</i>"
    )


def _briefing_intro(persona: PersonaProfile | None, now: datetime) -> str:
    if persona is None:
        return (
            "A sharp scan for what can affect your dev work, gear choices, "
            "game ideas, and IELTS practice."
        )
    return persona_intro(persona, now)


def _persona_quote(
    persona: PersonaProfile,
    text: str,
    *,
    with_name: bool = False,
) -> str:
    spoken_text = escape(text)
    if with_name:
        spoken_text = f"<b>{escape(persona.name)}:</b> {spoken_text}"
    return f"<blockquote>{spoken_text}</blockquote>"


def _inspiration_ideas(
    news_by_category: dict[str, list[BriefingNewsItem]],
    focus: str,
    now: datetime,
) -> tuple[str, str]:
    game_item = _first_category_item(news_by_category, "gaming") or _first_top_item(
        news_by_category
    )
    web_item = (
        _first_category_item(news_by_category, "web_tech")
        or _first_category_item(news_by_category, "governance")
        or _first_top_item(news_by_category)
    )
    return (
        _game_design_idea(game_item, focus, now),
        _web_design_idea(web_item, now),
    )


def _game_design_idea(
    item: BriefingNewsItem | None,
    focus: str,
    now: datetime,
) -> str:
    fallback = _daily_pick(GAME_IDEAS, now, offset=0)
    if item is None:
        return fallback

    text = _news_signal_text(item)
    headline = _headline_fragment(item, limit=46)
    if any(
        phrase in text
        for phrase in ("layoff", "layoffs", "closure", "job cuts", "job cut", "restructuring")
    ):
        mechanic = "Studio Scarcity"
        detail = (
            "each failed jump removes one helper object from the next attempt, "
            "turning resource pressure into readable horror tension"
        )
    elif any(word in text for word in ("switch", "console", "hardware", "sales")):
        mechanic = "Input Echo"
        detail = (
            "the monster copies the player's last movement rhythm, so safer platforming "
            "requires deliberately breaking habits"
        )
    elif any(word in text for word in ("ai", "agent", "tool", "model")):
        mechanic = "Predictive Haunt"
        detail = (
            "an AI-like ghost forecasts the player's safest route and blocks it unless "
            "the player improvises under pressure"
        )
    elif any(word in text for word in ("law", "policy", "regulation", "government")):
        mechanic = "Consent Gate"
        detail = (
            "doors demand a memory sacrifice before opening, making progress a choice "
            "between privacy and survival"
        )
    else:
        mechanic, _, detail = fallback.partition(": ")

    return f'{mechanic}: inspired by "{headline}", build a {focus} room where {detail}.'


def _web_design_idea(item: BriefingNewsItem | None, now: datetime) -> str:
    fallback = _daily_pick(WEB_IDEAS, now, offset=1)
    if item is None:
        return fallback

    text = _news_signal_text(item)
    headline = _headline_fragment(item, limit=46)
    if any(word in text for word in ("privacy", "security", "prompt injection", "redact")):
        pattern = "Trust Chips"
        detail = (
            "show source, risk, and confidence chips under each link so fast scanning "
            "still feels accountable"
        )
    elif any(word in text for word in ("ai", "agent", "tool", "model")):
        pattern = "Action-First Digest"
        detail = (
            "group each story by what you should do next: learn, build, watch, or ignore"
        )
    elif any(word in text for word in ("macbook", "laptop", "keyboard", "mobile", "gpu")):
        pattern = "Tactile Cards"
        detail = (
            "use compact cards with keyboard-like states: pressed, held, and released "
            "for filtering hardware signals"
        )
    elif any(word in text for word in ("law", "policy", "regulation", "government")):
        pattern = "Policy Timeline"
        detail = (
            "place regulations on a left-to-right timeline so impact and deadlines are "
            "visible before the summary"
        )
    else:
        pattern, _, detail = fallback.partition(": ")

    return f'{pattern}: inspired by "{headline}", {detail}.'


def _render_sentence_structures(
    news_by_category: dict[str, list[BriefingNewsItem]],
    focus: str,
) -> list[str]:
    top_items = _top_news_items(news_by_category, limit=3)
    primary = top_items[0] if top_items else None
    secondary = top_items[1] if len(top_items) > 1 else primary
    primary_theme = _theme_from_item(primary)
    secondary_theme = _theme_from_item(secondary)
    impact_area = _impact_area(primary.category if primary else "")
    headline = _headline_fragment(primary, limit=52)
    return [
        f"- <b>Cleft sentence:</b> What matters most for a Hanoi-based developer is <b>{escape(primary_theme)}</b>, not just the headline.",
        f"- <b>Inversion:</b> Rarely does <b>{escape(secondary_theme)}</b> move this quickly without changing {escape(impact_area)}.",
        f"- <b>Complex conditional:</b> Were <b>{escape(headline)}</b> to become a lasting trend, your {escape(_shorten(focus, 36))} could need a faster prototype loop.",
    ]


def _academic_vocabulary(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> tuple[tuple[str, str, str], ...]:
    text = _all_news_text(news_by_category)
    rules: tuple[tuple[tuple[str, ...], tuple[str, str, str]], ...] = (
        (
            ("ai", "agent", "model", "tool", "openai"),
            (
                "adoption",
                "sự áp dụng/chấp nhận",
                "AI adoption can shorten Godot prototyping, but the core fear loop still needs human judgement.",
            ),
        ),
        (
            ("privacy", "security", "prompt injection", "redact", "data"),
            (
                "mitigation",
                "sự giảm thiểu rủi ro",
                "Input validation is a mitigation strategy before using player text in a live game.",
            ),
        ),
        (
            ("macbook", "laptop", "keyboard", "mobile", "gpu", "hardware"),
            (
                "latency",
                "độ trễ",
                "Input latency can ruin a precision jump even when the level design is fair.",
            ),
        ),
        (
            ("switch", "game pass", "sales", "studio", "industry", "market"),
            (
                "monetization",
                "chiến lược kiếm tiền",
                "A horror-platformer can test monetization later, after the core loop feels strong.",
            ),
        ),
        (
            ("law", "policy", "regulation", "government", "compliance"),
            (
                "compliance",
                "sự tuân thủ quy định",
                "Compliance matters if a small studio collects analytics from players in different countries.",
            ),
        ),
        (
            ("vietnam", "hanoi", "trade", "economy", "business"),
            (
                "resilience",
                "khả năng phục hồi/chống chịu",
                "A resilient solo workflow keeps progress moving even when tools or markets shift.",
            ),
        ),
    )
    fallback_entries: tuple[tuple[str, str, str], ...] = (
        (
            "iteration",
            "quá trình lặp lại/cải tiến",
            "Fast iteration helps test a fear mechanic nightly.",
        ),
        (
            "constraint",
            "ràng buộc",
            "A constraint can make monster AI fairer.",
        ),
        (
            "trade-off",
            "sự đánh đổi",
            "The key trade-off is lighting vs. readability.",
        ),
        (
            "immersion",
            "sự nhập vai",
            "Immersion improves when sound, lighting, and controls support the same fear fantasy.",
        ),
        (
            "provenance",
            "nguồn gốc/xuất xứ",
            "Asset provenance matters when a horror game uses AI-generated textures.",
        ),
    )
    selected: list[tuple[str, str, str]] = []
    seen_words: set[str] = set()
    for keywords, entry in rules:
        if any(keyword in text for keyword in keywords):
            selected.append(entry)
            seen_words.add(entry[0])
    for entry in fallback_entries:
        if entry[0] in seen_words:
            continue
        selected.append(entry)
        seen_words.add(entry[0])
        if len(selected) >= 5:
            break
    return tuple(selected[:5])


def _teacher_challenge(
    news_by_category: dict[str, list[BriefingNewsItem]],
    location: str,
    focus: str,
) -> str:
    top_item = _first_top_item(news_by_category)
    theme = _theme_from_item(top_item)
    return (
        f"To what extent should developers in {location} adapt their {focus} plans "
        f"around {theme}, and where should they draw the line?"
    )


def _answer_frame(
    news_by_category: dict[str, list[BriefingNewsItem]],
    focus: str,
) -> str:
    theme = _theme_from_item(_first_top_item(news_by_category))
    return (
        f"Although {theme} can improve speed, I would prioritize {focus} quality "
        "because users notice weak execution immediately."
    )


def _band8_phrase(news_by_category: dict[str, list[BriefingNewsItem]]) -> str:
    theme = _theme_from_item(_first_top_item(news_by_category))
    if "safety" in theme or "regulation" in theme:
        return "a necessary trade-off between innovation and accountability"
    if "hardware" in theme:
        return "a marginal gain that compounds across the whole workflow"
    if "market" in theme:
        return "a signal of shifting consumer expectations"
    return "a practical catalyst for faster iteration"


def _top_news_items(
    news_by_category: dict[str, list[BriefingNewsItem]],
    limit: int,
) -> list[BriefingNewsItem]:
    items: list[BriefingNewsItem] = []
    for category_items in news_by_category.values():
        items.extend(item for item in category_items if item.impact_score > 1)
    return sorted(items, key=lambda item: item.impact_score, reverse=True)[:limit]


def _first_top_item(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> BriefingNewsItem | None:
    items = _top_news_items(news_by_category, limit=1)
    return items[0] if items else None


def _first_category_item(
    news_by_category: dict[str, list[BriefingNewsItem]],
    category: str,
) -> BriefingNewsItem | None:
    for item in news_by_category.get(category, []):
        if item.impact_score > 1:
            return item
    return None


def _theme_from_item(item: BriefingNewsItem | None) -> str:
    if item is None:
        return "today's developer signal"
    text = _news_signal_text(item)
    if _contains_any_term(text, ("privacy", "security", "redact")) or any(
        phrase in text for phrase in ("prompt injection",)
    ):
        return "AI safety and trust"
    if _contains_any_term(text, ("ai", "agent", "model", "tool", "openai")):
        return "AI-assisted production"
    if _contains_any_term(text, ("react", "devops", "github", "developer")):
        return "developer workflow velocity"
    if _contains_any_term(text, ("macbook", "laptop", "keyboard", "mobile", "gpu")):
        return "hardware choices and input quality"
    if _contains_any_term(text, ("switch", "sales", "studio", "layoff")) or "game pass" in text:
        return "game market pressure"
    if _contains_any_term(text, ("law", "policy", "regulation", "government")):
        return "technology regulation"
    return _headline_fragment(item, limit=42)


def _impact_area(category: str) -> str:
    return {
        "web_tech": "prototype speed, tool choice, and deployment risk",
        "hardware": "budgeting, test devices, and input quality",
        "gaming": "market positioning and mechanic selection",
        "governance": "compliance, payments, and platform risk",
    }.get(category, "daily planning")


def _headline_fragment(item: BriefingNewsItem | None, limit: int = 50) -> str:
    if item is None:
        return "today's developer news"
    return _shorten(_clean_headline(item.headline), limit)


def _clean_headline(headline: str) -> str:
    cleaned = re.sub(r"\s*\((?:HN(?: search)? fallback)\)\s*$", "", headline)
    return " ".join(cleaned.split())


def _news_text(item: BriefingNewsItem) -> str:
    return f"{item.headline} {item.summary} {item.source} {item.url}".lower()


def _news_signal_text(item: BriefingNewsItem) -> str:
    return f"{item.headline} {item.source} {item.url}".lower()


def _all_news_text(news_by_category: dict[str, list[BriefingNewsItem]]) -> str:
    return " ".join(
        _news_text(item)
        for category_items in news_by_category.values()
        for item in category_items
        if item.impact_score > 1
    )


def _next_project_step(focus: str, task: str) -> str:
    if "godot" in focus.lower():
        return (
            "Build one tiny playable scene; note fear, readability, and retry frustration."
        )
    return f"Timebox 45 minutes to finish or revise '{task}', then commit the smallest working slice."


def _daily_pick(options: tuple[str, ...], now: datetime, offset: int) -> str:
    index = (int(now.strftime("%j")) + offset) % len(options)
    return options[index]


def _first_headline(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> str:
    for items in news_by_category.values():
        if items:
            return items[0].headline
    return "today's developer news"


def _source_label(url: str) -> str:
    match = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return match.group(1) if match else "web"


def _table_escape(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _category_icon(category_key: str) -> str:
    return {
        "web_tech": "🧑‍💻",
        "hardware": "⌨️",
        "gaming": "🎮",
        "governance": "🌍",
    }.get(category_key, "📰")


def _impact_label(score: int) -> str:
    if score >= 8:
        label = "High"
    elif score >= 5:
        label = "Medium"
    else:
        label = "Low"
    return f"<b>[{score}/10 {label}]</b>"


def _impact_bar(score: int) -> str:
    score = max(0, min(10, score))
    filled = round(score / 2)  # 5-segment bar
    return "▰" * filled + "▱" * (5 - filled) + f" {score}/10"


_SUMMARY_BOILERPLATE = (
    "enter your email",
    "sign up",
    "subscribe",
    "newsletter",
    "confirmation",
    "cookie",
    "privacy policy",
    "latest gaming news first",
    "terms of service",
    "create an account",
)


def _clean_summary(summary: str) -> str:
    """Flatten a source snippet into one clean sentence-ish line.

    Drops obvious newsletter/cookie boilerplate that scrapers often capture
    instead of real article text.
    """
    text = unescape(re.sub(r"<[^>]+>", "", summary or ""))
    text = re.sub(r"\s+", " ", text).strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SUMMARY_BOILERPLATE):
        return ""
    return text


def _top_pick(
    news_by_category: dict[str, list[BriefingNewsItem]],
) -> BriefingNewsItem | None:
    best: BriefingNewsItem | None = None
    for items in news_by_category.values():
        for item in items:
            if item.impact_score <= 1:
                continue
            if best is None or item.impact_score > best.impact_score:
                best = item
    return best


def _rule() -> str:
    return "----------------------------------------"


def _shorten(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    clipped = value[: limit - 1]
    # Prefer a word boundary so we never cut mid-word or mid-number.
    space = clipped.rfind(" ")
    if space >= limit * 0.6:
        clipped = clipped[:space]
    return clipped.rstrip(" ,.;:-") + "…"


def _shorten_html(value: str, limit: int) -> str:
    return escape(_shorten(value, limit))


def _trim_briefing(text: str) -> str:
    if _visible_len(text) <= MAX_BRIEFING_CHARS:
        return text
    persistence_match = re.search(r"\[PERSISTENCE: .+\]$", text)
    persistence = persistence_match.group(0) if persistence_match else ""
    raw_limit = max(1200, MAX_BRIEFING_CHARS - len(persistence) - 40)
    body = text[:raw_limit].rstrip()
    suffix = "\n\nNote: briefing trimmed for Telegram."
    if persistence:
        suffix = f"{suffix}\n{persistence}"
    return f"{body}{suffix}"


def _visible_len(text: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))


def _make_key(category: str, url: str, headline: str) -> str:
    payload = f"{category}|{url}|{headline}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()
