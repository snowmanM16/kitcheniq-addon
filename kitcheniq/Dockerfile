# ARG BUILD_FROM allows HA Supervisor to inject the correct base image per architecture.
# Falls back to python:3.11-slim for standalone Docker use.
ARG BUILD_FROM=python:3.11-slim
FROM $BUILD_FROM

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is mounted by HA Supervisor (config.yaml map: data:rw)
# uploads/ is transient scratch space inside the container
RUN mkdir -p /data /data/image_cache /app/uploads

ENV PYTHONUNBUFFERED=1

CMD ["/bin/bash", "run.sh"]
