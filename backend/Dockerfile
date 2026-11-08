FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 REQUIRE_SERVER_DB=1 CLIENT_IP_HEADER=x-envoy-external-address
WORKDIR /app
COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt && useradd --uid 10001 --create-home bloomate
COPY app ./app
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log"]
