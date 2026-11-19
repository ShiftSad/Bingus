#!/bin/sh
# Pausa a coleta quando o banco chega perto do espaço reservado no NVMe e retoma quando houver
# folga de novo, depois de um reindex por exemplo. Só retoma o que ele mesmo pausou.
LIMITE_GB=${LIMITE_GB:-90}
FLAG=${XDG_RUNTIME_DIR:-/tmp}/bingus-pausado
gb=$(docker compose exec -T postgres du -s -BG /var/lib/postgresql | cut -dG -f1)
[ -n "$gb" ] || exit 1
if [ "$gb" -ge "$LIMITE_GB" ]; then
    echo "banco com $gb GB, limite $LIMITE_GB: pausando a coleta"
    systemctl --user stop 'bingus-fetch@*' && touch "$FLAG"
elif [ -e "$FLAG" ] && [ "$gb" -lt $((LIMITE_GB - 5)) ]; then
    echo "banco com $gb GB: retomando a coleta"
    workers=$(ls "$HOME/.config/systemd/user/default.target.wants" | grep '^bingus-fetch@')
    systemctl --user start $workers && rm -f "$FLAG"
else
    echo "banco com $gb GB"
fi
