#!/usr/bin/env bash
set -euo pipefail

# Локальная сборка образа и публикация в GitHub Container Registry.
# Usage: bash build.sh [--tag v1.2.0]
#
# Требуется GHCR_IMAGE, GHCR_USER, GHCR_TOKEN, HF_TOKEN в .env

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[•]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}══ $* ══${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Аргументы ─────────────────────────────────────────────────────────────────
IMAGE_TAG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag|-t) IMAGE_TAG="$2"; shift 2 ;;
        *) error "Неизвестный аргумент: $1" ;;
    esac
done

# ── Загрузка .env ─────────────────────────────────────────────────────────────
[[ -f "$SCRIPT_DIR/.env" ]] || error ".env не найден. Скопируйте .env.example и заполните переменные."
# shellcheck source=/dev/null
source "$SCRIPT_DIR/.env"

[[ -n "${GHCR_IMAGE:-}" ]]  || error "GHCR_IMAGE не задан в .env (пример: ghcr.io/username/speech-analytics-gpu-server)"
[[ -n "${GHCR_USER:-}" ]]   || error "GHCR_USER не задан в .env"
[[ -n "${GHCR_TOKEN:-}" ]]  || error "GHCR_TOKEN не задан в .env (нужен scope: write:packages)"
[[ -n "${HF_TOKEN:-}" ]]    || error "HF_TOKEN не задан в .env"

IMAGE_TAG="${IMAGE_TAG:-${IMAGE_TAG_DEFAULT:-latest}}"

echo -e "${BOLD}${BLUE}"
echo "  Speech Analytics GPU Server — Build & Push"
echo -e "${RESET}"
info "Образ: ${GHCR_IMAGE}:${IMAGE_TAG}"

# ── Авторизация в GHCR ────────────────────────────────────────────────────────
header "Авторизация"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
success "Авторизован как $GHCR_USER"

# ── Сборка ────────────────────────────────────────────────────────────────────
header "Сборка образа"
info "Это займёт 30–60 минут при первой сборке..."
IMAGE_TAG="$IMAGE_TAG" docker compose \
    -f "$SCRIPT_DIR/docker-compose.yml" \
    build \
    --build-arg "HF_TOKEN=$HF_TOKEN"
success "Образ собран"

# ── Публикация ────────────────────────────────────────────────────────────────
header "Публикация в GHCR"
IMAGE_TAG="$IMAGE_TAG" docker compose \
    -f "$SCRIPT_DIR/docker-compose.yml" \
    push api
success "Опубликован: ${GHCR_IMAGE}:${IMAGE_TAG}"

echo ""
echo -e "${GREEN}${BOLD}Готово.${RESET} Для деплоя на сервер:"
echo -e "  bash deploy-remote.sh user@server"
