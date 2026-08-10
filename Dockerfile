FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8081

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY gemini_web2api/ ./gemini_web2api/

EXPOSE 8081

# All configuration comes from environment variables (see .env.example).
# On Railway the injected PORT is picked up automatically.
CMD ["python", "-m", "gemini_web2api"]
