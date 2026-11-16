#!/bin/sh
set -eu

POSTGRES_USER=$(cat /run/postgres-bootstrap/user)
POSTGRES_DB=$(cat /run/postgres-bootstrap/database)
POSTGRES_PASSWORD_FILE=/run/postgres-bootstrap/password
export POSTGRES_USER POSTGRES_DB POSTGRES_PASSWORD_FILE

exec /usr/local/bin/docker-entrypoint.sh postgres
