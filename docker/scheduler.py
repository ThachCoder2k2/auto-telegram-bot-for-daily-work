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


def _next_run(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


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


def main() -> None:
    tz = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))
    hour = _env_int("DAILY_SEND_HOUR", 8, 0, 23)
    minute = _env_int("DAILY_SEND_MINUTE", 0, 0, 59)

    print(
        f"[scheduler] up. tz={tz.key} daily={hour:02d}:{minute:02d} "
        f"run_on_start={_env_flag('RUN_ON_START', False)}",
        flush=True,
    )

    if _env_flag("RUN_ON_START", False):
        _send_digest()

    while True:
        now = datetime.now(tz)
        target = _next_run(now, hour, minute)
        sleep_s = (target - now).total_seconds()
        print(
            f"[scheduler] next run {target.isoformat()} "
            f"(sleep {int(sleep_s)}s)",
            flush=True,
        )
        # Cap single sleep so a host clock/DST jump can't strand us for a full
        # day; re-check the target each wake.
        time.sleep(min(sleep_s, 3600))
        if datetime.now(tz) >= target:
            _send_digest()


if __name__ == "__main__":
    main()
