FROM python:3.12-alpine AS builder

WORKDIR /app

COPY requirements.txt .

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

FROM python:3.12-alpine

WORKDIR /app

RUN apk update && apk add --no-cache mosquitto-clients

COPY --from=builder /app/wheels /wheels

RUN pip install --no-cache --break-system-packages /wheels/*

COPY find_my.py healthcheck.py ./
RUN chmod +x healthcheck.py

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD /usr/bin/python3 healthcheck.py

LABEL org.opencontainers.image.source=https://github.com/watsona4/find_my

CMD ["python", "find_my.py"]
