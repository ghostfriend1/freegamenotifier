FROM python:3.12-slim

WORKDIR /app

# Pure-Python wheels are available for all deps, so no build toolchain needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Persist the SQLite dedup/stats database across restarts by mounting a volume
# here (e.g. `-v fgn-data:/app/data` and set DB_FILE=/app/data/free_games_v3.db),
# or mount over /app on platforms like Railway.
ENV DB_FILE=free_games_v3.db

CMD ["python", "main.py"]
