FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libgomp1 xvfb libgtk-3-0 libdbus-glib-1-2 libasound2t64 libx11-xcb1 \
      libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxcursor1 libxi6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import ddddocr; ddddocr.DdddOcr(show_ad=False)" \
 && python -m camoufox fetch
COPY app ./app
CMD ["python", "-m", "app.main"]
