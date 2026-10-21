FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    CHECKPOINT_DB=/tmp/checkpoints.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Run as a non-root user
RUN useradd --create-home app && chown -R app /app
USER app

# Cloud Run injects $PORT (8080 by default)
EXPOSE 8080
CMD streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-8080} --server.headless=true