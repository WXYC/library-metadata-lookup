#!/bin/bash
set -eo pipefail

LOG_FILE="$HOME/Library/Logs/library-metadata-lookup-etl.log"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SLACK_WEBHOOK_URL="${SLACK_MONITORING_WEBHOOK:-}"
NOTIFY_ENABLED=false
EXIT_CODE=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --notify)
            NOTIFY_ENABLED=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--notify]"
            exit 1
            ;;
    esac
done

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg"
}

notify_error() {
    local message="$1"
    log "ERROR: $message"

    if [[ "$NOTIFY_ENABLED" == "true" && -n "$SLACK_WEBHOOK_URL" ]]; then
        curl -s -X POST "$SLACK_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "{\"text\":\":warning: *Library ETL Failed*\n$message\"}" \
            >> "$LOG_FILE" 2>&1 || true
    fi
}

upload_library_db() {
    local url="$1"
    local label="$2"
    local db_path="$3"

    log "Uploading library.db to $label ($url)..."

    UPLOAD_OUTPUT=$(mktemp)
    HTTP_CODE=$(curl -s -o "$UPLOAD_OUTPUT" -w "%{http_code}" \
        -X POST "$url/admin/upload-library-db" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -F "file=@$db_path" \
        2>> "$LOG_FILE")

    if [[ "$HTTP_CODE" -eq 200 ]]; then
        ROW_COUNT=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('row_count','?'))" < "$UPLOAD_OUTPUT" 2>/dev/null || echo "?")
        log "Uploaded to $label successfully ($ROW_COUNT rows)"
        rm -f "$UPLOAD_OUTPUT"
        return 0
    else
        ERROR_BODY=$(cat "$UPLOAD_OUTPUT")
        rm -f "$UPLOAD_OUTPUT"
        notify_error "Upload to $label failed (HTTP $HTTP_CODE): $ERROR_BODY"
        return 1
    fi
}

cd "$REPO_DIR"

# Load environment variables from .env if it exists
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

# Validate required environment variables
if [[ -z "$ADMIN_TOKEN" ]]; then
    log "ERROR: ADMIN_TOKEN is required"
    exit 1
fi

log "Starting library sync"

# Run ETL, capturing output for error reporting
DB_PATH=$(mktemp -d)/library.db
export LIBRARY_DB_OUTPUT_PATH="$DB_PATH"

ETL_OUTPUT=$(mktemp)
if ! .venv/bin/python scripts/export_to_sqlite.py 2>&1 | tee "$ETL_OUTPUT"; then
    ERROR_DETAILS=$(grep -v '^[[:space:]]' "$ETL_OUTPUT" | grep -v '^$' | tail -1 | sed 's/"/\\"/g')
    cat "$ETL_OUTPUT" >> "$LOG_FILE"
    rm -f "$ETL_OUTPUT" "$DB_PATH"
    notify_error "ETL script failed: $ERROR_DETAILS"
    exit 1
fi
cat "$ETL_OUTPUT" >> "$LOG_FILE"
rm -f "$ETL_OUTPUT"

# Upload to staging (if URL configured)
if [[ -n "$STAGING_URL" ]]; then
    upload_library_db "$STAGING_URL" "staging" "$DB_PATH" || EXIT_CODE=1
fi

# Upload to production (if URL configured)
if [[ -n "$PRODUCTION_URL" ]]; then
    upload_library_db "$PRODUCTION_URL" "production" "$DB_PATH" || EXIT_CODE=1
fi

# Clean up
rm -f "$DB_PATH"
rmdir "$(dirname "$DB_PATH")" 2>/dev/null || true

if [[ $EXIT_CODE -eq 0 ]]; then
    log "Library sync completed successfully"
else
    log "Library sync completed with errors (see above)"
fi

exit $EXIT_CODE
