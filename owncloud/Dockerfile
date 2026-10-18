FROM owncloud/server:10.13.4

ARG OWNCLOUD_UID=504

RUN usermod --uid "${OWNCLOUD_UID}" www-data

COPY prepare-mirror-config.php /usr/local/bin/prepare-owncloud-mirror-config
COPY apache.conf /etc/apache2/sites-enabled/default.conf
RUN chmod 0755 /usr/local/bin/prepare-owncloud-mirror-config
