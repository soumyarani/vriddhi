# syntax=docker/dockerfile:1
FROM python:3.11-slim

# PYTHONDONTWRITEBYTECODE: no .pyc litter in a read-only-ish container layer.
# PYTHONUNBUFFERED: stdout/stderr stream straight to the docker log driver.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# fonts-dejavu-core is a hard runtime requirement, not an optional extra:
# app/services/invoice.py embeds DejaVuSans to render the rupee sign (U+20B9)
# on GST invoices. reportlab's built-in Type 1 fonts are WinAnsi-only, so
# without this package every invoice silently degrades to the "Rs." spelling.
# It installs to /usr/share/fonts/truetype/dejavu/, the first path the
# service probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied on its own so the dependency layer is reused whenever only app code
# changes. requirements.txt keeps its test dependencies below a "# testing"
# marker; strip from that marker to EOF so pytest/aiosqlite never reach the
# runtime image.
COPY requirements.txt ./
RUN sed '/^# testing/,$d' requirements.txt > /tmp/requirements-runtime.txt \
    && pip install --no-cache-dir -r /tmp/requirements-runtime.txt \
    && rm /tmp/requirements-runtime.txt

COPY . .

# Run unprivileged. Ownership is set in the same layer as user creation so the
# image does not carry a duplicate copy of /app with different ownership.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chmod +x /app/entrypoint.sh \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Uses the stdlib rather than adding curl to the image. Hits the readiness
# probe, which checks database and Redis connectivity as well as process
# liveness. start-period covers migration time on a cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready', timeout=4).status == 200 else 1)"

# Migrations, the arq worker and the API all start here. See entrypoint.sh for
# why they share a container; docker-compose.yml still runs them separately.
CMD ["/app/entrypoint.sh"]
