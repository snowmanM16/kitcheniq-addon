# HA Supervisor injects the correct base image (Alpine-based) per architecture via BUILD_FROM
ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# HA base images are Alpine — install Python 3, pip, and bash
RUN apk add --no-cache python3 py3-pip bash

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /data/image_cache /app/uploads \
    && sed -i 's/\r//' run.sh \
    && chmod +x run.sh

ENV PYTHONUNBUFFERED=1

CMD ["/bin/bash", "run.sh"]
