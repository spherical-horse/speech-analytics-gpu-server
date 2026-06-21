#!/usr/bin/env bash
set -euo pipefail

# ── Цвета ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[•]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}══ $* ══${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Проверки ───────────────────────────────────────────────────────────────────
check_root() {
    [[ $EUID -eq 0 ]] || error "Запустите скрипт от root: sudo bash deploy.sh"
}

check_os() {
    if [[ ! -f /etc/os-release ]]; then
        warn "Не удалось определить OS. Скрипт тестировался на Ubuntu 22.04."
        return
    fi
    # shellcheck source=/dev/null
    source /etc/os-release
    if [[ "$ID" != "ubuntu" ]]; then
        warn "Скрипт тестировался на Ubuntu. Текущая OS: $PRETTY_NAME"
    fi
}

check_compose_file() {
    [[ -f "$SCRIPT_DIR/docker-compose.yml" ]] \
        || error "docker-compose.yml не найден. Запустите скрипт из корня проекта."
}

# ── Docker ─────────────────────────────────────────────────────────────────────
install_docker() {
    if command -v docker &>/dev/null && docker compose version &>/dev/null; then
        success "Docker уже установлен ($(docker --version))"
        return
    fi
    info "Устанавливаю Docker..."
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    success "Docker установлен"
}

# ── NVIDIA ─────────────────────────────────────────────────────────────────────
check_nvidia_drivers() {
    if nvidia-smi &>/dev/null; then
        success "NVIDIA драйвер обнаружен ($(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1))"
        return 0
    fi

    warn "NVIDIA драйвер не обнаружен. Пробую установить через ubuntu-drivers..."
    apt-get install -y -q ubuntu-drivers-common
    ubuntu-drivers autoinstall || true

    if nvidia-smi &>/dev/null; then
        success "NVIDIA драйвер установлен"
    else
        echo ""
        warn "Драйвер установлен, но требуется перезагрузка."
        warn "Выполните: reboot && sudo bash deploy.sh"
        exit 0
    fi
}

install_nvidia_toolkit() {
    if docker run --rm --gpus all nvidia/cuda:12.6.1-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
        success "nvidia-container-toolkit уже настроен"
        return
    fi
    info "Устанавливаю nvidia-container-toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q
    apt-get install -y -q nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
    success "nvidia-container-toolkit установлен"
}

# ── Конфигурация ───────────────────────────────────────────────────────────────
collect_config() {
    header "Конфигурация"

    # Если .env уже есть — предложить использовать его
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        warn ".env уже существует."
        read -rp "  Использовать существующий .env? [Y/n] " USE_EXISTING
        if [[ "${USE_EXISTING,,}" != "n" ]]; then
            # shellcheck source=/dev/null
            source "$SCRIPT_DIR/.env"
            success "Использую существующий .env"
            return
        fi
    fi

    # Домен
    read -rp "  Домен (например, api.example.com) [localhost]: " DOMAIN
    DOMAIN="${DOMAIN:-localhost}"

    # HF токен
    while true; do
        read -rsp "  HuggingFace токен (hf_...): " HF_TOKEN
        echo
        [[ "$HF_TOKEN" == hf_* ]] && break
        warn "  Токен должен начинаться с hf_"
    done

    # Пароль БД
    read -rsp "  Пароль для PostgreSQL [авто]: " DB_PASSWORD
    echo
    if [[ -z "$DB_PASSWORD" ]]; then
        DB_PASSWORD="$(openssl rand -hex 24)"
        info "Сгенерирован пароль БД: ${BOLD}$DB_PASSWORD${RESET} (сохранён в .env)"
    fi

    export DOMAIN HF_TOKEN DB_PASSWORD
}

create_env_file() {
    info "Создаю .env..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|"             "$SCRIPT_DIR/.env"
    sed -i "s|^HF_TOKEN=.*|HF_TOKEN=$HF_TOKEN|"       "$SCRIPT_DIR/.env"
    sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$DB_PASSWORD|" "$SCRIPT_DIR/.env"
    chmod 600 "$SCRIPT_DIR/.env"
    success ".env создан"
}

# ── Docker-операции ────────────────────────────────────────────────────────────
build_images() {
    header "Сборка образов"
    info "Это займёт 30–60 минут при первой сборке (скачиваются ML-модели)..."
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" build \
        --build-arg "HF_TOKEN=$HF_TOKEN"
    success "Образы собраны"
}

start_services() {
    header "Запуск сервисов"
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
    info "Жду готовности PostgreSQL..."
    for i in $(seq 1 30); do
        if docker compose -f "$SCRIPT_DIR/docker-compose.yml" \
               exec -T postgres pg_isready -U app -d transcript &>/dev/null; then
            success "PostgreSQL готов"
            return
        fi
        sleep 2
    done
    error "PostgreSQL не поднялся за 60 секунд"
}

run_migrations() {
    header "Миграции БД"
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" \
        exec -T api alembic upgrade head
    success "Миграции применены"
}

create_admin_token() {
    header "Создание токена администратора"
    # CLI выводит токен на строке вида "  <token>" после "Value (save this...)"
    CLI_OUTPUT="$(
        docker compose -f "$SCRIPT_DIR/docker-compose.yml" \
            exec -T api python -m app.cli create_token --name admin
    )"
    # Токен — строка с ведущими пробелами, только URL-safe base64 символы
    ADMIN_TOKEN="$(echo "$CLI_OUTPUT" | grep -oP '^\s+\K[A-Za-z0-9_-]{60,}' || true)"
    success "Токен создан"
}

print_summary() {
    local PROTOCOL="https"
    [[ "$DOMAIN" == "localhost" ]] && PROTOCOL="https (self-signed)"

    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${RESET}"
    echo -e "${GREEN}${BOLD}║          Деплой завершён успешно                ║${RESET}"
    echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${RESET}"
    echo ""
    echo -e "  ${BOLD}URL:${RESET}          https://$DOMAIN"
    echo -e "  ${BOLD}Health:${RESET}       https://$DOMAIN/api/v1/health"
    echo -e "  ${BOLD}Swagger:${RESET}      https://$DOMAIN/docs"
    echo ""
    if [[ -n "${ADMIN_TOKEN:-}" ]]; then
        echo -e "  ${BOLD}${YELLOW}Токен API (сохраните — показывается один раз):${RESET}"
        echo -e "  ${BOLD}$ADMIN_TOKEN${RESET}"
    fi
    echo ""
    echo -e "  ${BOLD}Управление:${RESET}"
    echo -e "    docker compose logs -f api       # логи API"
    echo -e "    docker compose logs -f worker    # логи воркера"
    echo -e "    docker compose restart           # перезапуск"
    echo ""
    if [[ "$DOMAIN" == "localhost" ]]; then
        echo -e "  ${YELLOW}Self-signed сертификат: для теста добавьте --insecure к curl${RESET}"
        echo -e "  ${YELLOW}Для продакшна задайте DOMAIN=ваш-домен в .env и пересоберите${RESET}"
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    echo -e "${BOLD}${BLUE}"
    echo "  Speech Analytics GPU Server — Deploy"
    echo -e "${RESET}"

    check_root
    check_os
    check_compose_file

    header "Зависимости системы"
    install_docker
    check_nvidia_drivers
    install_nvidia_toolkit

    collect_config

    # Создаём .env только если собираем конфиг заново
    if [[ -z "${USE_EXISTING:-}" || "${USE_EXISTING,,}" == "n" ]]; then
        create_env_file
    fi

    build_images
    start_services
    run_migrations
    create_admin_token
    print_summary
}

main "$@"
