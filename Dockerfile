FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=10000 \
    QUOTE_CACHE_SECONDS=15 \
    YAHOO_HTTP_TIMEOUT=15

EXPOSE 10000
CMD ["sh", "-c", "uvicorn server_taifex:app --host 0.0.0.0 --port ${PORT}"]
