FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (needed for some discord.py builds)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY main.py .

# NO .env file is copied — Railway injects variables at runtime

# Run the bot
CMD ["python", "main.py"]
