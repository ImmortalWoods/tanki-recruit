#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin="$project_dir/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
    echo 'Сначала создайте .venv и установите зависимости по README.' >&2
    exit 1
fi
case "$project_dir" in *'%'*|*'"'*|*'\'*)
    echo 'Путь проекта содержит символы, которые установщик systemd не поддерживает.' >&2
    exit 1 ;;
esac
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/tanki-recruit.service" <<EOF
[Unit]
Description=Автоматические приглашения в клан Мир танков
ConditionPathExists=$project_dir/candidates.py
OnFailure=tanki-recruit-failure.service

[Service]
Type=oneshot
WorkingDirectory=$project_dir
ExecStart="$python_bin" -u "$project_dir/candidates.py" --send
TimeoutStartSec=infinity
EOF
cat > "$unit_dir/tanki-recruit.timer" <<'EOF'
[Unit]
Description=Ежедневный набор игроков в клан

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF
cat > "$unit_dir/tanki-recruit-failure.service" <<'EOF'
[Unit]
Description=Уведомление об ошибке набора в клан

[Service]
Type=oneshot
ExecStart=/usr/bin/notify-send -u critical "Набор в клан" "Ошибка отправки приглашений. Подробности: journalctl --user -u tanki-recruit.service"
EOF
systemctl --user daemon-reload
systemctl --user enable --now tanki-recruit.timer
echo 'Таймер включён. Проверка: systemctl --user list-timers tanki-recruit.timer'
