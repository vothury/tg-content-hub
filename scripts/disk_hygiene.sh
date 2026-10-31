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
FREE_MB=$(df -PM / | awk 'NR==2 {print $4}')
if [ "$FREE_MB" -lt 2048 ]; then
  # дефицит: кэш сборки и воспроизводимое сырьё БД уходят первыми
  docker builder prune -af || true
  gzip -9f backlog/facts_*.jsonl backlog/istoria.jsonl 2>/dev/null || true
fi
# медиа-том — главный потребитель между деплоями (~1 ГБ/неделю):
# чистим по защитному списку БД (опубликованные и очереди не трогаются)
docker compose run --rm --entrypoint "python -m app.tools.clean_media_volume" api >/dev/null 2>&1 \
  || echo "clean_media: пропущено (стек недоступен)"
if [ "$1" = "deep" ]; then
  docker builder prune -f
fi
echo "--- после очистки:"
df -h / | tail -1