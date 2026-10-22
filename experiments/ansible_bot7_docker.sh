#!/usr/bin/env bash
# Проверка Прочее/bot7/ansible/playbook.yml на «чистом сервере» — контейнере ubuntu:24.04
# (в образе нет python3, sudo, git, acl). Плейбук запускается дважды: второй раз ничего не меняет.
#
#   ANSIBLE=/path/to/venv/bin experiments/ansible_bot7_docker.sh
#   SOURCE=git GIT_REPO=https://github.com/... GIT_VERSION=ветка experiments/ansible_bot7_docker.sh
#
# systemd в контейнере не запущен, поэтому bot7_manage_service=false: юнит службы только
# записывается. Он проверяется systemd-analyze verify, а его ExecStart запускается от
# пользователя gran: bot7_server.py стартует бота, тот с фиктивными токенами не подключается
# к ВК и падает, сервер перезапускает его — всё видно в logs/bot7.log.
#
# Нужны docker, ansible-core и коллекция community.docker (ansible-galaxy collection install community.docker).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PLAY="$HERE/../Прочее/bot7/ansible/playbook.yml"
BIN="${ANSIBLE:+$ANSIBLE/}"
SOURCE="${SOURCE:-local}"
NAME="bot7-ansible-test-$$"
TMP="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

docker run -d --name "$NAME" ubuntu:24.04 sleep infinity >/dev/null
if [ "$SOURCE" = git ]; then DIR=/opt/gran/repo/Прочее/bot7; else DIR=/opt/gran/bot7; fi

mkdir -p "$TMP/книги"
printf 'Глава 1. Сократ\n\nСократ учил, что добродетель есть знание.\n' > "$TMP/книги/Сократ.txt"
cat > "$TMP/inventory.ini" <<EOF
[bot7]
$NAME ansible_connection=community.docker.docker
EOF
cat > "$TMP/secrets.yml" <<EOF
vk_group_id: "5"
vk_token: "vk-test-token"
proxyapi_key: "sk-test"
admin_ids: "7, 8"
bot7_manage_service: false
bot7_rag_src: "$TMP/книги"
bot7_source: "$SOURCE"
bot7_git_repo: "${GIT_REPO:-https://github.com/PavelChurkin/ai_mind_test1.git}"
bot7_git_version: "${GIT_VERSION:-main}"
EOF

run() { "${BIN}ansible-playbook" -i "$TMP/inventory.ini" "$PLAY" -e @"$TMP/secrets.yml" "$@"; }

echo "=== запуск 1: чистый сервер ==="
run | tee "$TMP/run1.log"
echo "=== запуск 2: повтор — ничего не меняется ==="
run | tee "$TMP/run2.log"
grep -Eq "changed=0 +unreachable=0 +failed=0" "$TMP/run2.log" || { echo "FAIL: повтор что-то изменил"; exit 1; }

echo "=== проверки на сервере ==="
d() { docker exec "$NAME" "$@"; }
d id gran
d grep -q "ExecStart=/opt/gran/venv/bin/python $DIR/bot7_server.py --restart-days 7" /etc/systemd/system/gran-bot7.service
d grep -q "^User=gran" /etc/systemd/system/gran-bot7.service
[ "$(d stat -c '%U %a' "$DIR/.env")" = "gran 600" ] || { echo "FAIL: права .env"; exit 1; }
d grep -q "^VK_TOKEN=vk-test-token" "$DIR/.env"
d grep -q "^ADMIN_IDS=7, 8" "$DIR/.env"
d test -f "$DIR/RAG/Сократ.txt"
d test -f "$DIR/Роль.txt"
# бот и база знаний работают из установленного окружения от пользователя службы
d sudo -u gran /opt/gran/venv/bin/python "$DIR/bot7.py" --help | grep -q -- "--no-terminal-stop"
d sudo -u gran sh -c "cd '$DIR' && /opt/gran/venv/bin/python -c '
import vk_api, openai, dotenv, pymorphy3, pypdf, olefile, rag
kb = rag.KnowledgeBase(\"RAG\", \"/tmp/rag_index_test.db\")
kb.sync()
hits = kb.search(\"чему учил Сократ\")
assert hits and \"добродетель\" in hits[0].text, hits
print(\"РАГ на сервере:\", kb.lemmatizer.name, hits)
'"

echo "=== служба: systemd-analyze verify и запуск ExecStart от gran ==="
d sh -c "apt-get install -y -q systemd >/dev/null 2>&1"     # только ради systemd-analyze
d systemd-analyze verify /etc/systemd/system/gran-bot7.service
EXEC="$(d sed -n 's/^ExecStart=//p' /etc/systemd/system/gran-bot7.service)"
d sh -c "cd '$DIR' && timeout -s INT 20 sudo -u gran $EXEC" > "$TMP/service.log" 2>&1 || true
cat "$TMP/service.log" | tail -n 6
d test -f "$DIR/logs/bot7.log" || { echo "FAIL: нет $DIR/logs/bot7.log"; exit 1; }
[ "$(d stat -c '%U' "$DIR/logs/bot7.log")" = gran ] || { echo "FAIL: лог не от gran"; exit 1; }
echo "--- logs/bot7.log ---"; d tail -n 12 "$DIR/logs/bot7.log"
d grep -q "SERVER запуск: " "$DIR/logs/bot7.log" || { echo "FAIL: сервер не запускал бота"; exit 1; }
echo OK
