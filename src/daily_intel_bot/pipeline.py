from __future__ import annotations

from daily_intel_bot.collectors import (
    collect_hacker_news,
    collect_rss_items,
    collect_tavily_items,
    collect_world_bank_stats,
)
from daily_intel_bot.briefing import build_daily_briefing
from daily_intel_bot.config import Settings
from daily_intel_bot.digest import render_digest
from daily_intel_bot.models import DigestBundle
from daily_intel_bot.ranking import rank_items, select_with_recent_filter
from daily_intel_bot.state_store import StateStore


def build_digest_bundle(settings: Settings) -> DigestBundle:
    enabled_collectors = {value.strip().lower() for value in settings.enabled_collectors}
    enabled_sections = {value.strip().lower() for value in settings.enabled_sections}

    signal_items: list = []
    if "hacker_news" in enabled_collectors:
        signal_items.extend(collect_hacker_news(limit=settings.hacker_news_limit))
    if "rss" in enabled_collectors:
        signal_items.extend(
            collect_rss_items(
                limit_per_feed=settings.rss_limit_per_feed,
                enabled_feed_names={
                    value.strip().lower() for value in settings.enabled_rss_feeds
                },
            )
        )
    if "tavily" in enabled_collectors:
        signal_items.extend(collect_tavily_items(settings))

    ranked_items = rank_items(signal_items)
    state_store = StateStore(settings.state_db_path)
    recent_sent = state_store.recent_sent_keys()

    tech_ranked = [item for item in ranked_items if item.topic == "tech"]
    ai_ranked = [item for item in ranked_items if item.topic == "ai"]

    tech_items = []
    if "tech" in enabled_sections and settings.tech_item_limit > 0:
        tech_items = select_with_recent_filter(
            tech_ranked,
            recent_sent,
            limit=settings.tech_item_limit,
        )

    ai_items = []
    if "ai" in enabled_sections and settings.ai_item_limit > 0:
        ai_items = select_with_recent_filter(
            ai_ranked,
            recent_sent,
            limit=settings.ai_item_limit,
        )

    selected_keys = {item.key for item in tech_items + ai_items}
    watchlist_pool = [item for item in ranked_items if item.key not in selected_keys]

    watchlist_items = []
    if "watchlist" in enabled_sections and settings.watchlist_item_limit > 0:
        watchlist_items = select_with_recent_filter(
            watchlist_pool,
            recent_sent,
            limit=settings.watchlist_item_limit,
        )

    selected_items = tech_items + ai_items + watchlist_items
    stats_items = []
    if "stats" in enabled_sections and "world_bank" in enabled_collectors:
        stats_items = collect_world_bank_stats()

    return DigestBundle(
        tech_items=tech_items,
        ai_items=ai_items,
        watchlist_items=watchlist_items,
        stats_items=stats_items,
        selected_items=selected_items,
    )


def build_digest_text(settings: Settings) -> tuple[str, DigestBundle]:
    if settings.briefing_mode == "dev_ielts":
        text, selected_items, vocabulary = build_daily_briefing(settings)
        bundle = DigestBundle(
            tech_items=[],
            ai_items=[],
            watchlist_items=[],
            stats_items=[],
            selected_items=selected_items,
            vocabulary=vocabulary,
        )
        return text, bundle

    bundle = build_digest_bundle(settings)
    text = render_digest(
        bundle,
        settings.timezone,
        enabled_sections=settings.enabled_sections,
    )
    return text, bundle
