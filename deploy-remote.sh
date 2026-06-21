#!/usr/bin/env bash
set -euo pipefail

# Usage: bash deploy-remote.sh user@host [-i /path/to/key]

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[•]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}══ $* ══${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="${1:?Использование: bash deploy-remote.sh user@host [-i /path/to/key]}"
shift

SSH_KEY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i) SSH_KEY="$2"; shift 2 ;;
        *) error "Неизвестный аргумент: $1" ;;
    esac
done

REMOTE_DIR="/opt/speech-analytics"
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
[[ -n "$SSH_KEY" ]] && SSH_OPTS="$SSH_OPTS -i $SSH_KEY"

command -v rsync &>/dev/null || error "rsync не найден. Установите: apt install rsync / brew install rsync"

echo -e "${BOLD}${BLUE}"
echo "  Speech Analytics GPU Server — Remote Deploy"
echo -e "${RESET}"
info "Цель: $TARGET:$REMOTE_DIR"

# ── 1. Синхронизация файлов ────────────────────────────────────────────────────
header "Синхронизация файлов"

info "Копирую файлы проекта..."
# shellcheck disable=SC2086
rsync -az --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='*.egg-info' \
    -e "ssh $SSH_OPTS" \
    "$SCRIPT_DIR/" "$TARGET:$REMOTE_DIR/"
success "Файлы синхронизированы"

# Синхронизация .env если есть локально
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    read -rp "  Синхронизировать локальный .env на сервер? [y/N] " SYNC_ENV
    if [[ "${SYNC_ENV,,}" == "y" ]]; then
        # shellcheck disable=SC2086
        rsync -az -e "ssh $SSH_OPTS" "$SCRIPT_DIR/.env" "$TARGET:$REMOTE_DIR/.env"
        success ".env синхронизирован"
    fi
fi

# ── 2. Запуск деплоя на сервере ───────────────────────────────────────────────
header "Деплой на сервере"

# shellcheck disable=SC2086
HAS_TMUX=$(ssh $SSH_OPTS "$TARGET" "command -v tmux &>/dev/null && echo yes || echo no" 2>/dev/null || echo no)

if [[ "$HAS_TMUX" == "yes" ]]; then
    info "tmux найден — деплой запускается в tmux-сессии."
    info "Если соединение оборвётся, переподключитесь и выполните: tmux attach -t deploy"
    echo ""
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$TARGET" "tmux kill-session -t deploy 2>/dev/null || true"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS -tt "$TARGET" \
        "tmux new-session -s deploy 'cd $REMOTE_DIR && sudo bash deploy.sh; echo; read -rp \"Нажмите Enter для выхода...\" _'"
else
    warn "tmux не найден на сервере. Деплой запускается напрямую."
    warn "Сборка образа займёт 30–60 мин. Если соединение оборвётся — запустите снова."
    echo ""
    # shellcheck disable=SC2086
    ssh $SSH_OPTS -tt "$TARGET" "cd $REMOTE_DIR && sudo bash deploy.sh"
fi
