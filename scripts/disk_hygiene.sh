#!/bin/sh
# Гигиена диска сервера: журналы и docker-мусор.
# Безопасно: не трогает builder-кэш, используемые образы и том postgres.
set -e
journalctl --vacuum-size=200M 2>/dev/null || sudo journalctl --vacuum-size=200M
docker image prune -f
docker container prune -f
echo "--- после очистки:"
df -h / | tail -1