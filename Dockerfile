FROM python:3.12-slim

# tzdata so ZoneInfo(TIMEZONE) resolves (Asia/Ho_Chi_Minh etc).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching, then the package itself.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Runtime assets the app reads via Path.cwd().
COPY assets ./assets
COPY docker/scheduler.py ./docker/scheduler.py

# state/ holds the SQLite db + briefing_state.json; mount as a volume to persist.
RUN mkdir -p state

CMD ["python", "docker/scheduler.py"]
