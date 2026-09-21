"""Command handlers for the two-way Telegram loop.

Every command mutates durable state — ``briefing_state.json`` for the task and
focus fields, the SQLite store for streak history, mutes, topic feedback and
vocabulary reviews — so the next digest reflects the reply.

Handlers are pure with respect to I/O ordering: they read state, write state,
and return a ``CommandResult``. Actually sending anything is the caller's job,
which keeps this module testable without a network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from daily_intel_bot.briefing import (
    BriefingState,
    load_briefing_state,
    save_briefing_state,
)
from daily_intel_bot import notion_tasks
from daily_intel_bot.config import Settings
from daily_intel_bot.notion_client import NotionError
from daily_intel_bot.obs import get_logger
from daily_intel_bot.reminders import render_task_line
from daily_intel_bot.state_store import StateStore, current_streak


_LOG = get_logger("commands")

# Bare-word aliases, because the digest footer tells the user to reply with
# "done / blocked / change task" rather than with slash commands.
BARE_ALIASES = {
    "done": "/done",
    "blocked": "/blocked",
    "change task": "/task",
    "status": "/status",
    "help": "/help",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    reply: str
    # Side effects the caller must perform; handlers never send on their own.
    action: str = ""


def handle_command(settings: Settings, text: str) -> CommandResult:
    """Route one inbound message to its handler."""
    name, argument = _split_command(text)
    if not name:
        return CommandResult(reply=_help_text())

    handler = _HANDLERS.get(name)
    if handler is None:
        return CommandResult(
            reply=f"❓ Unknown command <code>{escape(name)}</code>.\n\n{_help_text()}"
        )
    try:
        return handler(settings, argument)
    except Exception as exc:  # noqa: BLE001 - never let one bad reply kill the poller
        _LOG.exception("command %s failed", name)
        return CommandResult(
            reply=f"💥 <b>{escape(name)}</b> failed: {escape(type(exc).__name__)}: "
            f"{escape(str(exc))}"
        )


def _split_command(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if not cleaned:
        return "", ""
    lowered = cleaned.lower()
    for alias, command in BARE_ALIASES.items():
        if lowered == alias or lowered.startswith(f"{alias} "):
            cleaned = f"{command} {cleaned[len(alias):]}".strip()
            break
    if not cleaned.startswith("/"):
        # Free text with no command is treated as a note on the current task.
        return "/note", cleaned
    head, _, rest = cleaned.partition(" ")
    # Group chats deliver "/done@MyBot"; drop the mention suffix.
    name = head.split("@", 1)[0].lower()
    return name, rest.strip()


# --- task flow ------------------------------------------------------------


def _cmd_done(settings: Settings, argument: str) -> CommandResult:
    store = StateStore(settings.state_db_path)
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    if not tasks:
        return CommandResult(reply="📭 No pending task to close. Add one with /task.")

    finished = tasks[0]
    store.record_task_event(finished, "done", argument, _now(settings))
    remaining = tasks[1:]
    streak = current_streak(store.done_days(today=_today(settings)), _today(settings))
    save_briefing_state(
        settings,
        BriefingState(
            last_date=state.last_date,
            tasks_remaining=remaining,
            current_project_focus=state.current_project_focus,
            streak=streak,
            # Finishing the task clears whatever was blocking it.
            blocked_reason="",
            next_micro_task="",
        ),
    )
    lines = [
        f"✅ Closed: <b>{escape(finished)}</b>",
        f"🔥 Streak: <b>{streak}</b> day(s)",
    ]
    if argument:
        lines.append(f"📝 Note: {escape(argument)}")
    lines.append(
        f"🎯 Next up: <b>{escape(remaining[0])}</b>"
        if remaining
        else "📭 Task list is empty — add the next one with /task &lt;text&gt;."
    )
    return CommandResult(reply="\n".join(lines))


def _cmd_blocked(settings: Settings, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(
            reply="✍️ Tell me what blocks you: <code>/blocked out of art assets</code>"
        )
    store = StateStore(settings.state_db_path)
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    current = tasks[0] if tasks else state.current_project_focus
    store.record_task_event(current, "blocked", argument, _now(settings))
    save_briefing_state(
        settings,
        BriefingState(
            last_date=state.last_date,
            tasks_remaining=tasks,
            current_project_focus=state.current_project_focus,
            streak=state.streak,
            blocked_reason=argument,
            next_micro_task=state.next_micro_task,
        ),
    )
    return CommandResult(
        reply=(
            f"🧱 Logged blocker on <b>{escape(current)}</b>:\n"
            f"<i>{escape(argument)}</i>\n\n"
            "Tomorrow's brief will lead with unblocking this."
        )
    )


def _cmd_task(settings: Settings, argument: str) -> CommandResult:
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    if not argument:
        if not tasks:
            return CommandResult(reply="📭 No pending tasks.")
        listed = "\n".join(
            f"{index}. {escape(task)}" for index, task in enumerate(tasks, start=1)
        )
        return CommandResult(reply=f"📋 <b>Pending tasks</b>\n{listed}")

    # A new task goes to the front: it is what the user just decided to do.
    updated = [argument] + [task for task in tasks if task != argument]
    save_briefing_state(
        settings,
        BriefingState(
            last_date=state.last_date,
            tasks_remaining=updated,
            current_project_focus=state.current_project_focus,
            streak=state.streak,
            blocked_reason="",
            next_micro_task="",
        ),
    )
    return CommandResult(
        reply=f"🎯 Now tracking: <b>{escape(argument)}</b>\n({len(updated)} task(s) queued)"
    )


def _cmd_drop(settings: Settings, argument: str) -> CommandResult:
    store = StateStore(settings.state_db_path)
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    if not tasks:
        return CommandResult(reply="📭 Nothing to drop.")
    dropped = tasks[0]
    store.record_task_event(dropped, "dropped", argument, _now(settings))
    save_briefing_state(
        settings,
        BriefingState(
            last_date=state.last_date,
            tasks_remaining=tasks[1:],
            current_project_focus=state.current_project_focus,
            streak=state.streak,
            blocked_reason="",
            next_micro_task="",
        ),
    )
    return CommandResult(reply=f"🗑️ Dropped: <b>{escape(dropped)}</b> (no streak change)")


def _cmd_focus(settings: Settings, argument: str) -> CommandResult:
    state = load_briefing_state(settings)
    if not argument:
        return CommandResult(
            reply=f"🎯 Current focus: <b>{escape(state.current_project_focus)}</b>"
        )
    save_briefing_state(
        settings,
        BriefingState(
            last_date=state.last_date,
            tasks_remaining=state.tasks_remaining,
            current_project_focus=argument,
            streak=state.streak,
            blocked_reason=state.blocked_reason,
            next_micro_task=state.next_micro_task,
        ),
    )
    return CommandResult(
        reply=(
            f"🎯 Focus set to <b>{escape(argument)}</b>.\n"
            "News ranking and the Inspiration Lab will follow it from the next brief."
        )
    )


def _cmd_note(settings: Settings, argument: str) -> CommandResult:
    store = StateStore(settings.state_db_path)
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    current = tasks[0] if tasks else state.current_project_focus
    store.record_task_event(current, "note", argument, _now(settings))
    return CommandResult(reply=f"📝 Noted against <b>{escape(current)}</b>.")


# --- ranking feedback -----------------------------------------------------


def _cmd_skip(settings: Settings, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(
            reply=(
                "🔇 Mute a source or word:\n"
                "<code>/skip nbcnews.com</code> or <code>/skip crypto</code>"
            )
        )
    store = StateStore(settings.state_db_path)
    kind = "domain" if "." in argument and " " not in argument else "keyword"
    store.add_mute(kind, argument)
    return CommandResult(
        reply=f"🔇 Muted {kind} <b>{escape(argument.lower())}</b>. It won't be ranked again."
    )


def _cmd_unmute(settings: Settings, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(reply="✍️ Usage: <code>/unmute nbcnews.com</code>")
    removed = StateStore(settings.state_db_path).remove_mute(argument)
    if not removed:
        return CommandResult(reply=f"🤷 <b>{escape(argument)}</b> was not muted.")
    return CommandResult(reply=f"🔊 Unmuted <b>{escape(argument.lower())}</b>.")


def _cmd_mutes(settings: Settings, argument: str) -> CommandResult:
    mutes = StateStore(settings.state_db_path).list_mutes()
    if not mutes:
        return CommandResult(reply="🔊 Nothing muted.")
    listed = "\n".join(f"- [{kind}] {escape(value)}" for kind, value in mutes)
    return CommandResult(reply=f"🔇 <b>Muted</b>\n{listed}")


def _cmd_more(settings: Settings, argument: str) -> CommandResult:
    return _topic_feedback(settings, argument, delta=0.5, verb="boosted")


def _cmd_less(settings: Settings, argument: str) -> CommandResult:
    return _topic_feedback(settings, argument, delta=-0.5, verb="damped")


def _topic_feedback(
    settings: Settings,
    argument: str,
    delta: float,
    verb: str,
) -> CommandResult:
    topic = argument.strip().lower()
    valid = {value.strip().lower() for value in settings.enabled_categories}
    if topic not in valid:
        listed = ", ".join(sorted(valid))
        return CommandResult(reply=f"✍️ Pick a category: {escape(listed)}")
    score = StateStore(settings.state_db_path).bump_topic_feedback(topic, delta)
    return CommandResult(
        reply=f"⚖️ <b>{escape(topic)}</b> {verb} (weight {score:+.1f})."
    )


# --- vocabulary -----------------------------------------------------------


def _cmd_quiz(settings: Settings, argument: str) -> CommandResult:
    store = StateStore(settings.state_db_path)
    due = store.due_vocabulary(limit=3)
    total, mastered = store.vocabulary_stats()
    if not due:
        return CommandResult(
            reply=(
                f"🎓 Nothing due. {total} word(s) stored, {mastered} mastered.\n"
                "New words arrive with tomorrow's brief."
            )
        )
    lines = ["🧠 <b>Recall check</b> — meaning first, then reveal:"]
    for entry in due:
        lines.append(f"\n• <b>{escape(entry.word)}</b> (box {entry.box}/5)")
        lines.append(f"  <tg-spoiler>{escape(entry.meaning)}</tg-spoiler>")
    lines.append(
        "\nScore it: <code>/got &lt;word&gt;</code> or <code>/missed &lt;word&gt;</code>"
    )
    return CommandResult(reply="\n".join(lines))


def _cmd_got(settings: Settings, argument: str) -> CommandResult:
    return _review_word(settings, argument, correct=True)


def _cmd_missed(settings: Settings, argument: str) -> CommandResult:
    return _review_word(settings, argument, correct=False)


def _review_word(settings: Settings, argument: str, correct: bool) -> CommandResult:
    if not argument:
        return CommandResult(reply="✍️ Usage: <code>/got pervasive</code>")
    entry = StateStore(settings.state_db_path).review_vocabulary(argument, correct)
    if entry is None:
        return CommandResult(reply=f"🤷 <b>{escape(argument)}</b> is not in your deck.")
    if correct:
        return CommandResult(
            reply=(
                f"✅ <b>{escape(entry.word)}</b> → box {entry.box}/5 "
                f"({entry.correct} right / {entry.wrong} wrong)"
            )
        )
    return CommandResult(
        reply=(
            f"🔁 <b>{escape(entry.word)}</b> back to box 1 — due again tomorrow.\n"
            f"<i>{escape(entry.meaning)}</i>\n{escape(entry.example)}"
        )
    )


# --- Notion ---------------------------------------------------------------


def _cmd_ntasks(settings: Settings, argument: str) -> CommandResult:
    if not notion_tasks.is_configured(settings):
        return CommandResult(
            reply=(
                "🔌 Notion is off. Set NOTION_ENABLED, NOTION_TOKEN "
                "and NOTION_DATABASE_ID."
            )
        )
    store = StateStore(settings.state_db_path)
    try:
        board = notion_tasks.load_board(settings, store)
    except NotionError as exc:
        return CommandResult(reply=f"📛 Notion: {escape(str(exc))}")

    if not board.tasks:
        return CommandResult(reply="🎉 Board is clear — nothing open.")

    shown = board.tasks[: settings.notion_task_limit]
    # Persist the order so /ndone <n> still resolves after this message
    # scrolls out of view.
    notion_tasks.remember_task_order(store, shown)
    lines = [f"📥 <b>Notion — {len(board.tasks)} open</b>", ""]
    lines.extend(render_task_line(index, task) for index, task in enumerate(shown, 1))
    if len(board.tasks) > len(shown):
        lines.append(f"<i>+{len(board.tasks) - len(shown)} more</i>")
    lines.append("")
    lines.append("Tick one off with <code>/ndone &lt;n&gt;</code>")
    return CommandResult(reply="\n".join(lines))


def _cmd_ndone(settings: Settings, argument: str) -> CommandResult:
    if not notion_tasks.is_configured(settings):
        return CommandResult(reply="🔌 Notion is off.")
    try:
        number = int(argument.strip())
    except (TypeError, ValueError):
        return CommandResult(
            reply="✍️ Usage: <code>/ndone 2</code> — the number comes from /ntasks"
        )

    store = StateStore(settings.state_db_path)
    page_id = notion_tasks.resolve_task_number(store, number)
    if not page_id:
        return CommandResult(
            reply=f"🤷 No task #{number}. Run /ntasks for a fresh list."
        )
    try:
        board = notion_tasks.load_board(settings, store)
        notion_tasks.complete_task(settings, board.schema, page_id)
    except NotionError as exc:
        return CommandResult(reply=f"📛 Notion: {escape(str(exc))}")

    title = next(
        (task.title for task in board.tasks if task.page_id == page_id),
        f"task #{number}",
    )
    return CommandResult(
        reply=f"✅ Marked <b>{escape(title)}</b> as Done in Notion."
    )


# --- reporting ------------------------------------------------------------


def _cmd_status(settings: Settings, argument: str) -> CommandResult:
    store = StateStore(settings.state_db_path)
    state = load_briefing_state(settings)
    tasks = state.tasks_remaining
    streak = current_streak(store.done_days(today=_today(settings)), _today(settings))
    counts = store.task_event_counts(within_days=7, today=_today(settings))
    total_vocab, mastered = store.vocabulary_stats()
    due = len(store.due_vocabulary(limit=99))

    lines = [
        "📊 <b>Status</b>",
        f"🎯 Focus: <b>{escape(state.current_project_focus)}</b>",
        f"🔥 Streak: <b>{streak}</b> day(s)",
        f"📋 Pending: {len(tasks)} task(s)",
    ]
    if tasks:
        lines.append(f"   → <b>{escape(tasks[0])}</b>")
    if state.blocked_reason:
        lines.append(f"🧱 Blocked: <i>{escape(state.blocked_reason)}</i>")
    lines.append(
        "📈 Last 7d: "
        f"{counts.get('done', 0)} done · {counts.get('blocked', 0)} blocked · "
        f"{counts.get('dropped', 0)} dropped"
    )
    lines.append(f"🎓 Vocab: {total_vocab} stored · {mastered} mastered · {due} due")

    trends = store.topic_counts(within_days=7)
    if trends:
        listed = ", ".join(f"{topic} {count}" for topic, count in trends[:4])
        lines.append(f"🧭 Topics 7d: {escape(listed)}")
    mutes = store.list_mutes()
    if mutes:
        lines.append(f"🔇 Muted: {len(mutes)}")
    return CommandResult(reply="\n".join(lines))


def _cmd_digest(settings: Settings, argument: str) -> CommandResult:
    return CommandResult(reply="📨 Building today's brief…", action="send_digest")


def _cmd_help(settings: Settings, argument: str) -> CommandResult:
    return CommandResult(reply=_help_text())


def _help_text() -> str:
    return "\n".join(
        [
            "🤖 <b>Commands</b>",
            "",
            "<b>Task flow</b>",
            "/done [note] — close the current task, extend the streak",
            "/blocked &lt;reason&gt; — log what stopped you",
            "/task [text] — list tasks, or put a new one on top",
            "/drop — abandon the current task without breaking the streak",
            "/focus &lt;text&gt; — change the project lens",
            "",
            "<b>Tuning the feed</b>",
            "/skip &lt;domain|word&gt; — mute a source or topic",
            "/unmute &lt;value&gt; · /mutes",
            "/more &lt;category&gt; · /less &lt;category&gt;",
            "",
            "<b>IELTS</b>",
            "/quiz — review words that are due",
            "/got &lt;word&gt; · /missed &lt;word&gt;",
            "",
            "<b>Notion</b>",
            "/ntasks — list unfinished tasks from the board",
            "/ndone &lt;n&gt; — tick task n as Done in Notion",
            "",
            "<b>Other</b>",
            "/status — streak, tasks, trends",
            "/digest — send today's brief now",
            "",
            "<i>Plain text with no command is filed as a note on the current task.</i>",
        ]
    )


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _today(settings: Settings):
    return _now(settings).date()


_HANDLERS = {
    "/done": _cmd_done,
    "/blocked": _cmd_blocked,
    "/task": _cmd_task,
    "/drop": _cmd_drop,
    "/focus": _cmd_focus,
    "/note": _cmd_note,
    "/skip": _cmd_skip,
    "/unmute": _cmd_unmute,
    "/mutes": _cmd_mutes,
    "/more": _cmd_more,
    "/less": _cmd_less,
    "/quiz": _cmd_quiz,
    "/got": _cmd_got,
    "/missed": _cmd_missed,
    "/ntasks": _cmd_ntasks,
    "/ndone": _cmd_ndone,
    "/status": _cmd_status,
    "/digest": _cmd_digest,
    "/help": _cmd_help,
    "/start": _cmd_help,
}
