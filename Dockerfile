FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .

# The script loops internally (RUN_INTERVAL_HOURS), so no cron needed —
# just keep the container running.
CMD ["python", "-u", "sync.py"]
