FROM python:3.12-alpine AS builder

WORKDIR /app

COPY requirements.txt .

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

FROM python:3.12-alpine

WORKDIR /app

RUN apk update && apk add --no-cache mosquitto-clients

COPY --from=builder /app/wheels /wheels

RUN pip install --no-cache --break-system-packages /wheels/*

COPY find_my.py healthcheck.sh ./

HEALTHCHECK CMD ./healthcheck.sh

LABEL org.opencontainers.image.source=https://github.com/watsona4/find_my

CMD ["python", "find_my.py"]
