#!/bin/sh
# Гигиена диска сервера: журналы, docker-мусор, отчёт.
# Применение: ./scripts/disk_hygiene.sh [deep]
#   deep — дополнительно чистит buildkit-кэш (следующие сборки дольше: apt/pip скачаются заново).
# Безопасно: не трогает используемые образы и тома postgres/media.
set -e

WANT="[Journal]
SystemMaxUse=100M"
CONF=/etc/systemd/journald.conf.d/size.conf
if [ "$(cat "$CONF" 2>/dev/null)" != "$WANT" ]; then
  mkdir -p /etc/systemd/journald.conf.d 2>/dev/null || sudo mkdir -p /etc/systemd/journald.conf.d
  printf '%s\n' "$WANT" > "$CONF" 2>/dev/null || printf '%s\n' "$WANT" | sudo tee "$CONF" >/dev/null
  systemctl restart systemd-journald 2>/dev/null || sudo systemctl restart systemd-journald
fi
journalctl --vacuum-size=100M 2>/dev/null || sudo journalctl --vacuum-size=100M

docker image prune -f
docker container prune -f
if [ "$1" = "deep" ]; then
  docker builder prune -f
fi
echo "--- после очистки:"
df -h / | tail -1