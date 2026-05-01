# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# Custom Nextcloud image – production-apache base, multi-arch (amd64 + arm64)
# ─────────────────────────────────────────────────────────────────────────────
FROM nextcloud:production-apache

# Install additional PHP extensions / tools you may need
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ghostscript \
        imagemagick \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Raise default PHP limits (can still be overridden via env vars in compose)
RUN { \
        echo "memory_limit = \${PHP_MEMORY_LIMIT:-512M}"; \
        echo "upload_max_filesize = \${PHP_UPLOAD_LIMIT:-10G}"; \
        echo "post_max_size = \${PHP_UPLOAD_LIMIT:-10G}"; \
        echo "max_execution_time = 3600"; \
        echo "max_input_time = 3600"; \
    } > /usr/local/etc/php/conf.d/nextcloud-custom.ini

# Volumes are declared here for documentation; actual bind-mount paths
# come from docker-compose.yml via NEXTCLOUD_DATA_DIR
VOLUME ["/var/www/html/data", "/var/www/html/config", "/var/www/html/custom_apps"]

EXPOSE 80

