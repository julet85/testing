ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv \
    gcc \
    python3-dev \
    musl-dev \
    libffi-dev \
    openssl-dev \
    tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY webapp/ ./webapp/
COPY run.sh /run.sh

RUN chmod +x /run.sh

CMD ["/run.sh"]
