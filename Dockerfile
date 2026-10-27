# One image for both services (`apidrift serve` and `apidrift worker`). No git, no compilers:
# every dependency ships a wheel, and customer code is fetched as a tarball, never cloned.
# Pinned at or above 3.13.4: that release carries the tarfile ``filter="data"`` fixes the checkout relies on.
FROM python:3.13.5-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APIDRIFT_DATA=/data

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin apidrift \
    && mkdir -p /data && chown apidrift:apidrift /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

USER apidrift
EXPOSE 8000
CMD ["apidrift", "serve", "--host", "0.0.0.0", "--port", "8000"]
