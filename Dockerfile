FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt
COPY foodbot ./foodbot
CMD ["sh", "-c", "exec uvicorn foodbot.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
