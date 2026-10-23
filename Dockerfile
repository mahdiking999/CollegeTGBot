# ============================================================================
# Bilingual PDF Translation Engine — Google Cloud Run
# ============================================================================
# Deploy: gcloud run deploy bilingual-pdf --source engine/ --region us-central1
# ============================================================================

FROM python:3.11-slim

WORKDIR /app

# PyMuPDF wheels on Linux are self-contained, but we need gcc for some deps.
# We also pre-create the fonts dir so the app can write to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (layer cached unless requirements.txt changes) ────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application ───────────────────────────────────────────────────────────
COPY app.py .
RUN mkdir -p /app/fonts

# ── Runtime env ───────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/tmp
ENV ARABIC_FONT_DIR=/app/fonts
# MAX_PAGES and MAX_UPLOAD_MB can be overridden at deploy time
ENV MAX_PAGES=40
ENV MAX_UPLOAD_MB=20

# Cloud Run injects PORT=8080; the app reads it automatically.
EXPOSE 8080

CMD ["python", "app.py"]
