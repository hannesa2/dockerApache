# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# Custom Apache + PHP image – multi-arch (linux/amd64 + linux/arm64)
# All web content and config come from bind-mounts at runtime.
# ─────────────────────────────────────────────────────────────────────────────
FROM php:8.3-apache

# Install PHP extensions commonly needed with MySQL + Apache
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpng-dev \
        libjpeg-dev \
        libwebp-dev \
        libzip-dev \
        libonig-dev \
        libxml2-dev \
        curl \
        unzip \
        procps \
    && docker-php-ext-configure gd --with-jpeg --with-webp \
    && docker-php-ext-install -j"$(nproc)" \
        gd \
        mysqli \
        pdo \
        pdo_mysql \
        zip \
        mbstring \
        opcache \
        xml \
    && rm -rf /var/lib/apt/lists/*

# Enable useful Apache modules
RUN a2enmod rewrite headers deflate expires

# PHP production settings
RUN mv "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"

# Custom PHP limits (overridden at runtime via APACHE_DATA_DIR/config/php-custom.ini)
RUN { \
        echo "memory_limit = 256M"; \
        echo "upload_max_filesize = 512M"; \
        echo "post_max_size = 512M"; \
        echo "max_execution_time = 300"; \
    } > "$PHP_INI_DIR/conf.d/apache-custom.ini"

# Remove the default vhost – a real one is bind-mounted from outside
RUN rm /etc/apache2/sites-enabled/000-default.conf

EXPOSE 80 443

# Web root and Apache config come from outside via docker-compose volumes
VOLUME ["/var/www/html", "/etc/apache2/sites-enabled"]

