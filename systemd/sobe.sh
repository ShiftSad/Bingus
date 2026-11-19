#!/bin/sh
# No boot o Docker rootless restaura os containers antes da WireGuard subir, e porta publicada num IP
# que ainda não existe impede o container de iniciar. Espera os IPs dos binds do .env e liga o que
# ficou parado. --no-recreate: no boot só liga, nunca aplica mudança de configuração.
ips=$(grep -E '^BINGUS_[A-Z_]+_BIND=' .env | cut -d= -f2 | cut -d: -f1 | sort -u)
for _ in $(seq 150); do
    falta=""
    for ip in $ips; do
        ip -o addr | grep -q " $ip/" || falta="$falta $ip"
    done
    [ -z "$falta" ] && exec docker compose --profile workers up -d --no-recreate
    sleep 2
done
echo "sem interface para$falta depois de 5 min"
exit 1
