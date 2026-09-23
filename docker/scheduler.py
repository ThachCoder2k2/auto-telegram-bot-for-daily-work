"""Daily scheduler for the Clawbot digest.

Runs inside the container. Sleeps until the next configured send time in the
project timezone, runs `daily-intel-bot --send-digest`, then repeats. Pure
stdlib so no extra dependencies are needed.

Env knobs:
- TIMEZONE            IANA tz, default Asia/Ho_Chi_Minh (shared with the app)
- DAILY_SEND_HOUR     0-23, default 8
- DAILY_SEND_MINUTE   0-59, default 0
- RUN_ON_START        true/false, send once immediately on boot (default false)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw.strip())))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def now_past_target(tz: ZoneInfo, hour: int, minute: int) -> bool:
    """True when today's send time is already behind us."""
    now = datetime.now(tz)
    return now > now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_run(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _catch_up_if_missed() -> None:
    """Send today's brief if its time has passed and it never went out.

    Restarting after the send time used to skip the whole day, so a redeploy
    at 10:00 silently cost that day's brief. The send is idempotent — it
    checks the recorded delivery stamp — so trying on every start is safe.
    """
    print("[scheduler] checking whether today's brief was missed", flush=True)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "daily_intel_bot.main",
            "--send-digest",
            "--if-missed",
        ],
        check=False,
    )
    print(f"[scheduler] catch-up exit={proc.returncode}", flush=True)


def _send_digest() -> int:
    # Retry on failure: transient DNS/network errors are common right after a
    # laptop wake, when the container resumes before networking is ready.
    attempts = _env_int("SEND_RETRIES", 3, 1, 10)
    backoff = _env_int("SEND_RETRY_BACKOFF", 60, 1, 3600)
    last_rc = 1
    for attempt in range(1, attempts + 1):
        print(
            f"[scheduler] sending digest at {datetime.now().isoformat()} "
            f"(attempt {attempt}/{attempts})",
            flush=True,
        )
        proc = subprocess.run(
            [sys.executable, "-m", "daily_intel_bot.main", "--send-digest"],
            check=False,
        )
        last_rc = proc.returncode
        print(f"[scheduler] send-digest exit={last_rc}", flush=True)
        if last_rc == 0:
            return 0
        if attempt < attempts:
            print(f"[scheduler] retry in {backoff}s", flush=True)
            time.sleep(backoff)
    print(f"[scheduler] send-digest gave up after {attempts} attempts", flush=True)
    return last_rc


def _start_command_loop() -> subprocess.Popen | None:
    """Run the Telegram command poller beside the scheduler.

    A separate process rather than a thread: a wedged HTTP call in the poller
    then cannot take the daily send down with it, and the supervisor below can
    simply restart it.
    """
    if not _env_flag("COMMANDS_ENABLED", True):
        print("[scheduler] command loop disabled", flush=True)
        return None
    print("[scheduler] starting command loop", flush=True)
    return subprocess.Popen([sys.executable, "-m", "daily_intel_bot.main", "--serve"])


def _supervise(poller: subprocess.Popen | None) -> subprocess.Popen | None:
    """Restart the command loop if it exited."""
    if poller is None or poller.poll() is None:
        return poller
    print(
        f"[scheduler] command loop exited ({poller.returncode}); restarting",
        flush=True,
    )
    return _start_command_loop()


def main() -> None:
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))
    hour = _env_int("DAILY_SEND_HOUR", 8, 0, 23)
    minute = _env_int("DAILY_SEND_MINUTE", 0, 0, 59)

    print(
        f"[scheduler] up. tz={tz.key} daily={hour:02d}:{minute:02d} "
        f"run_on_start={_env_flag('RUN_ON_START', False)}",
        flush=True,
    )

    poller = _start_command_loop()

    if _env_flag("RUN_ON_START", False):
        _send_digest()
    elif _env_flag("CATCH_UP_ON_START", True) and now_past_target(tz, hour, minute):
        _catch_up_if_missed()

    try:
        while True:
            now = datetime.now(tz)
            target = _next_run(now, hour, minute)
            sleep_s = (target - now).total_seconds()
            print(
                f"[scheduler] next run {target.isoformat()} "
                f"(sleep {int(sleep_s)}s)",
                flush=True,
            )
            # Cap single sleep so a host clock/DST jump can't strand us for a
            # full day, and so the poller is checked on regularly.
            time.sleep(min(sleep_s, 300))
            poller = _supervise(poller)
            if datetime.now(tz) >= target:
                _send_digest()
    finally:
        if poller is not None and poller.poll() is None:
            poller.terminate()


if __name__ == "__main__":
    main()
