# Source image for the langgraph-chromium team template (base: langgraph-base).
# It adds headless Chromium and its system libraries; see README.md.
#
#   docker build --platform linux/amd64 -t registry.digitalocean.com/<your-registry>/langgraph-chromium:v1 template
#   docker push registry.digitalocean.com/<your-registry>/langgraph-chromium:v1
#   doctl harness-runtime template create --name langgraph-chromium --base-template langgraph-base \
#     --source-oci-ref registry.digitalocean.com/<your-registry>/langgraph-chromium:v1
#
# The template keeps /ms-playwright, its system libraries and this image's ENV.
# It does not keep Python packages installed here: the graph's packages come
# from requirements.txt at session start.
FROM ubuntu:24.04

# Must equal the playwright pin in ../requirements.txt.
ARG PLAYWRIGHT_VERSION=1.63.0

ENV DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip ca-certificates \
 && pip install --no-cache-dir --break-system-packages "playwright==${PLAYWRIGHT_VERSION}" \
 && python3 -m playwright install --with-deps --only-shell chromium \
 && chmod -R a+rwX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*
