#!/bin/sh
set -eu

psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=db_name="$OWNCLOUD_DB_NAME" \
    --set=db_user="$OWNCLOUD_DB_USER" \
    --set=db_password="$OWNCLOUD_DB_PASSWORD" <<'SQL'
CREATE ROLE :"db_user" LOGIN PASSWORD :'db_password';
CREATE DATABASE :"db_name" OWNER :"db_user";
SQL
