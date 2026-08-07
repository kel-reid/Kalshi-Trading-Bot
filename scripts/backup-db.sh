#!/usr/bin/env bash

# Database Backup Script for Kalshi Trading Bot PostgreSQL container.
# This script is meant to run on the host Droplet (e.g. via a daily cron job).

set -euo pipefail

# --- Configuration ---
# Target directory for backups on the host
BACKUP_DIR="/root/backups/kalshi-bot"
# Number of days to keep backups locally
KEEP_DAYS=7
# Env file location to load database configurations
ENV_FILE="/root/Kalshi-Trading-Bot/.env"

echo "=== Starting PostgreSQL Backup: $(date) ==="

# 1. Load database credentials from the .env file if it exists
if [ -f "$ENV_FILE" ]; then
    # Parse env file ignoring comments and exporting variables
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-kalshi_bot}"
DB_CONTAINER_NAME="kalshi-bot-db"

# 2. Ensure the host backup directory exists
mkdir -p "$BACKUP_DIR"

# 3. Define output filename with timestamp
BACKUP_FILE="${BACKUP_DIR}/backup_${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql.gz"

echo "Backing up database '${DB_NAME}' from container '${DB_CONTAINER_NAME}'..."

# 4. Perform transaction-consistent pg_dump inside the docker container
# Gzip the output stream on the host directly to save space
if ! docker exec -t "$DB_CONTAINER_NAME" pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$BACKUP_FILE"; then
    echo "ERROR: Backup failed!" >&2
    exit 1
fi

echo "Backup created successfully: ${BACKUP_FILE}"
echo "Size: $(du -sh "$BACKUP_FILE" | cut -f1)"

# 5. Clean up old backups (older than 7 days) to prevent disk space bloat
echo "Pruning backups older than ${KEEP_DAYS} days..."
find "$BACKUP_DIR" -type f -name "backup_${DB_NAME}_*.sql.gz" -mtime +"$KEEP_DAYS" -exec rm -f {} \; -print

echo "=== Backup Process Completed: $(date) ==="
