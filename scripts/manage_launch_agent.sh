#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

LABEL=${LABEL:-com.guohai.esim-switchboard}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}
WORK_DIR=${WORK_DIR:-"${ROOT_DIR}"}
PYTHON_BIN=${PYTHON_BIN:-"${ROOT_DIR}/.venv/bin/python"}
ADB_PATH=${ADB_PATH:-}
FFMPEG_PATH=${FFMPEG_PATH:-}
ADB_DEVICE_SERIAL=${ADB_DEVICE_SERIAL:-}
APP_PASSWORD=${APP_PASSWORD:-}
DB_PATH=${DB_PATH:-}
APP_AUTH_COOKIE_NAME=${APP_AUTH_COOKIE_NAME:-}
ADB_HEALTHCHECK_TIMEOUT_SECONDS=${ADB_HEALTHCHECK_TIMEOUT_SECONDS:-}
SMS_SYNC_DELAY_SECONDS=${SMS_SYNC_DELAY_SECONDS:-}
ADB_RECONNECT_DELAY_SECONDS=${ADB_RECONNECT_DELAY_SECONDS:-}
DEFAULT_PAGE_SIZE=${DEFAULT_PAGE_SIZE:-}
MAX_PAGE_SIZE=${MAX_PAGE_SIZE:-}
SMS_EVENT_POLL_INTERVAL_SECONDS=${SMS_EVENT_POLL_INTERVAL_SECONDS:-}
SWITCH_SCREENSHOT_DIR=${SWITCH_SCREENSHOT_DIR:-}
SWITCH_STEP_DELAY_SECONDS=${SWITCH_STEP_DELAY_SECONDS:-}
SWITCH_CONFIRM_WAIT_SECONDS=${SWITCH_CONFIRM_WAIT_SECONDS:-}

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs"
STDOUT_LOG=${STDOUT_LOG:-"${LOG_DIR}/esim-switchboard.log"}
STDERR_LOG=${STDERR_LOG:-"${LOG_DIR}/esim-switchboard.err.log"}
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
LAUNCH_DOMAIN="gui/$(id -u)"
SERVICE_TARGET="${LAUNCH_DOMAIN}/${LABEL}"

usage() {
    cat <<'EOF'
Usage:
  scripts/manage_launch_agent.sh install [options]
  scripts/manage_launch_agent.sh reload [options]
  scripts/manage_launch_agent.sh restart
  scripts/manage_launch_agent.sh status
  scripts/manage_launch_agent.sh uninstall
  scripts/manage_launch_agent.sh print-plist

Options:
  --label VALUE
  --host VALUE
  --port VALUE
  --python-bin PATH
  --work-dir PATH
  --adb-path PATH
  --ffmpeg-path PATH
  --adb-device-serial VALUE
  --app-password VALUE
  --db-path PATH
  --app-auth-cookie-name VALUE
  --stdout-log PATH
  --stderr-log PATH
  --adb-healthcheck-timeout-seconds VALUE
  --sms-sync-delay-seconds VALUE
  --adb-reconnect-delay-seconds VALUE
  --default-page-size VALUE
  --max-page-size VALUE
  --sms-event-poll-interval-seconds VALUE
  --switch-screenshot-dir PATH
  --switch-step-delay-seconds VALUE
  --switch-confirm-wait-seconds VALUE

Examples:
  APP_PASSWORD='secret123' scripts/manage_launch_agent.sh install
  scripts/manage_launch_agent.sh reload --port 18000
  scripts/manage_launch_agent.sh status
  scripts/manage_launch_agent.sh uninstall
EOF
}

xml_escape() {
    local value=${1:-}
    value=${value//&/&amp;}
    value=${value//</&lt;}
    value=${value//>/&gt;}
    printf '%s' "$value"
}

append_env_var() {
    local key=$1
    local value=$2
    if [[ -z "$value" ]]; then
        return
    fi
    cat <<EOF
        <key>$(xml_escape "$key")</key>
        <string>$(xml_escape "$value")</string>
EOF
}

resolve_defaults() {
    if [[ -z "${ADB_PATH}" ]]; then
        if command -v adb >/dev/null 2>&1; then
            ADB_PATH=$(command -v adb)
        elif [[ -x "${HOME}/Library/Android/sdk/platform-tools/adb" ]]; then
            ADB_PATH="${HOME}/Library/Android/sdk/platform-tools/adb"
        fi
    fi
}

require_prereqs() {
    if [[ ! -x "${PYTHON_BIN}" ]]; then
        echo "Missing Python interpreter: ${PYTHON_BIN}" >&2
        echo "Create the virtualenv first, for example: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
        exit 1
    fi
    if ! command -v launchctl >/dev/null 2>&1; then
        echo "launchctl is not available on this system." >&2
        exit 1
    fi
    if [[ -z "${ADB_PATH}" ]]; then
        echo "ADB_PATH is not set and adb was not found automatically." >&2
        echo "Run with --adb-path /absolute/path/to/adb or export ADB_PATH first." >&2
        exit 1
    fi
    if [[ ! -x "${ADB_PATH}" ]]; then
        echo "ADB binary is not executable: ${ADB_PATH}" >&2
        exit 1
    fi
}

write_plist() {
    mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
    cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml_escape "${LABEL}")</string>

    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "${PYTHON_BIN}")</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>$(xml_escape "${HOST}")</string>
        <string>--port</string>
        <string>$(xml_escape "${PORT}")</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(xml_escape "${WORK_DIR}")</string>

    <key>EnvironmentVariables</key>
    <dict>
$(append_env_var "ADB_PATH" "${ADB_PATH}")
$(append_env_var "FFMPEG_PATH" "${FFMPEG_PATH}")
$(append_env_var "ADB_DEVICE_SERIAL" "${ADB_DEVICE_SERIAL}")
$(append_env_var "APP_PASSWORD" "${APP_PASSWORD}")
$(append_env_var "DB_PATH" "${DB_PATH}")
$(append_env_var "APP_AUTH_COOKIE_NAME" "${APP_AUTH_COOKIE_NAME}")
$(append_env_var "ADB_HEALTHCHECK_TIMEOUT_SECONDS" "${ADB_HEALTHCHECK_TIMEOUT_SECONDS}")
$(append_env_var "SMS_SYNC_DELAY_SECONDS" "${SMS_SYNC_DELAY_SECONDS}")
$(append_env_var "ADB_RECONNECT_DELAY_SECONDS" "${ADB_RECONNECT_DELAY_SECONDS}")
$(append_env_var "DEFAULT_PAGE_SIZE" "${DEFAULT_PAGE_SIZE}")
$(append_env_var "MAX_PAGE_SIZE" "${MAX_PAGE_SIZE}")
$(append_env_var "SMS_EVENT_POLL_INTERVAL_SECONDS" "${SMS_EVENT_POLL_INTERVAL_SECONDS}")
$(append_env_var "SWITCH_SCREENSHOT_DIR" "${SWITCH_SCREENSHOT_DIR}")
$(append_env_var "SWITCH_STEP_DELAY_SECONDS" "${SWITCH_STEP_DELAY_SECONDS}")
$(append_env_var "SWITCH_CONFIRM_WAIT_SECONDS" "${SWITCH_CONFIRM_WAIT_SECONDS}")
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$(xml_escape "${STDOUT_LOG}")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "${STDERR_LOG}")</string>
</dict>
</plist>
EOF
    chmod 0644 "${PLIST_PATH}"
}

bootout_if_loaded() {
    launchctl bootout "${SERVICE_TARGET}" >/dev/null 2>&1 || true
    launchctl bootout "${LAUNCH_DOMAIN}" "${PLIST_PATH}" >/dev/null 2>&1 || true
}

install_agent() {
    resolve_defaults
    require_prereqs
    write_plist
    bootout_if_loaded
    launchctl bootstrap "${LAUNCH_DOMAIN}" "${PLIST_PATH}"
    launchctl enable "${SERVICE_TARGET}"
    launchctl kickstart -k "${SERVICE_TARGET}"
    echo "Installed LaunchAgent: ${LABEL}"
    echo "Plist: ${PLIST_PATH}"
    echo "Open: http://${HOST}:${PORT}/"
}

restart_agent() {
    launchctl kickstart -k "${SERVICE_TARGET}"
    echo "Restarted LaunchAgent: ${LABEL}"
}

status_agent() {
    launchctl print "${SERVICE_TARGET}"
}

uninstall_agent() {
    bootout_if_loaded
    rm -f "${PLIST_PATH}"
    echo "Removed LaunchAgent: ${LABEL}"
}

if [[ $# -lt 1 ]]; then
    usage
    exit 1
fi

COMMAND=$1
shift

if [[ "${COMMAND}" == "-h" || "${COMMAND}" == "--help" || "${COMMAND}" == "help" ]]; then
    usage
    exit 0
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --label)
            LABEL=$2
            shift 2
            ;;
        --host)
            HOST=$2
            shift 2
            ;;
        --port)
            PORT=$2
            shift 2
            ;;
        --python-bin)
            PYTHON_BIN=$2
            shift 2
            ;;
        --work-dir)
            WORK_DIR=$2
            shift 2
            ;;
        --adb-path)
            ADB_PATH=$2
            shift 2
            ;;
        --ffmpeg-path)
            FFMPEG_PATH=$2
            shift 2
            ;;
        --adb-device-serial)
            ADB_DEVICE_SERIAL=$2
            shift 2
            ;;
        --app-password)
            APP_PASSWORD=$2
            shift 2
            ;;
        --db-path)
            DB_PATH=$2
            shift 2
            ;;
        --app-auth-cookie-name)
            APP_AUTH_COOKIE_NAME=$2
            shift 2
            ;;
        --stdout-log)
            STDOUT_LOG=$2
            shift 2
            ;;
        --stderr-log)
            STDERR_LOG=$2
            shift 2
            ;;
        --adb-healthcheck-timeout-seconds)
            ADB_HEALTHCHECK_TIMEOUT_SECONDS=$2
            shift 2
            ;;
        --sms-sync-delay-seconds)
            SMS_SYNC_DELAY_SECONDS=$2
            shift 2
            ;;
        --adb-reconnect-delay-seconds)
            ADB_RECONNECT_DELAY_SECONDS=$2
            shift 2
            ;;
        --default-page-size)
            DEFAULT_PAGE_SIZE=$2
            shift 2
            ;;
        --max-page-size)
            MAX_PAGE_SIZE=$2
            shift 2
            ;;
        --sms-event-poll-interval-seconds)
            SMS_EVENT_POLL_INTERVAL_SECONDS=$2
            shift 2
            ;;
        --switch-screenshot-dir)
            SWITCH_SCREENSHOT_DIR=$2
            shift 2
            ;;
        --switch-step-delay-seconds)
            SWITCH_STEP_DELAY_SECONDS=$2
            shift 2
            ;;
        --switch-confirm-wait-seconds)
            SWITCH_CONFIRM_WAIT_SECONDS=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

case "${COMMAND}" in
    install|reload)
        install_agent
        ;;
    restart)
        restart_agent
        ;;
    status)
        status_agent
        ;;
    uninstall)
        uninstall_agent
        ;;
    print-plist)
        resolve_defaults
        require_prereqs
        write_plist
        cat "${PLIST_PATH}"
        ;;
    *)
        echo "Unknown command: ${COMMAND}" >&2
        usage
        exit 1
        ;;
esac
