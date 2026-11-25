# Отдельный App Connector в Docker

Этот шаблон создаёт `ai-egress-amsterdam` как **новое tagged-устройство Tailscale** со своей идентичностью, состоянием и сетевым namespace. Существующая пользовательская идентичность Tailscale на хосте сохраняется. До развёртывания прочитайте [план подготовки, проверки и отката](../docs/ISOLATED-CONNECTOR.md).

`compose.yaml` требует официальный `tailscale/tailscale@sha256:…` из внешнего env-файла без значения по умолчанию, использует kernel TUN, `/dev/net/tun`, `NET_ADMIN` и `NET_RAW`. Лимиты обычного Docker Compose: 128 PID, 512 MiB RAM, 1 CPU, перезапуск `unless-stopped`. Собственный bridge получает обязательный проверенный IPv4 CIDR из внешнего env-файла. IPv4 forwarding включается только в namespace контейнера; IPv6 forwarding выключен, у bridge нет IPv6.

Шаблон не использует host network, published ports, privileged, `SYS_MODULE`, Docker socket, состояние или LocalAPI socket хостового Tailscale. Kernel TUN должен уже существовать на хосте; отсутствие устройства — причина остановить подготовку. Создание bridge всё равно добавляет правила Docker в host netfilter, поэтому изоляция не означает нулевой риск для существующих сервисов. Docker описывает это поведение в [документации bridge](https://docs.docker.com/engine/network/drivers/bridge/).

Стартует непосредственно `tailscaled`, без `containerboot` и автоматического `tailscale up`. На пустом состоянии он ожидает отдельного входа владельца. `TS_USERSPACE=false` фиксирует намерение, а `--tun=tailscale0` задаёт режим демону; `TS_STATE_DIR` и `TS_SOCKET` явно передаются ему аргументами. `TS_AUTHKEY`, `TS_EXTRA_ARGS` и другие настройки `containerboot` здесь не используются. После личного browser login сохранённое состояние позволяет возобновить ту же идентичность при рестарте. Механизм wrapper описан в [официальном исходном коде v1.104.7](https://github.com/tailscale/tailscale/blob/v1.104.7/cmd/containerboot/main.go).

Проверка синтаксиса из корня репозитория, **без создания сети, контейнера, каталога state или входа в tailnet**:

```bash
TAILSCALE_IMAGE=tailscale/tailscale@sha256:0000000000000000000000000000000000000000000000000000000000000000 \
CONNECTOR_STATE_DIR=/opt/tailscale-ai-egress/state \
CONNECTOR_IPV4_SUBNET=10.255.254.0/28 \
CONNECTOR_DNS_IPV4_PRIMARY=1.1.1.1 \
CONNECTOR_DNS_IPV4_SECONDARY=9.9.9.9 \
docker compose --env-file /dev/null --project-name ai-egress-amsterdam -f hub/compose.yaml config --quiet
```

Все значения этой команды предназначены только для локального разбора YAML. Нулевой digest не является рабочим image; доступность CIDR и DNS на целевом хосте не проверена. `--env-file /dev/null` исключает зависимость этой проверки от локального `.env`. Для развёртывания задайте проверенные официальный image digest, свой CIDR и оба DNS во внешней копии `connector.env.example`. Пустые image, CIDR, DNS или отсутствующий state path останавливают Compose до создания ресурсов. `config` не подтверждает kernel TUN, права, доступность image, маршрут или центральную policy.

Запуск, личный вход и включение connector — отдельные действия администратора, приведённые в [инструкции](../docs/ISOLATED-CONNECTOR.md). У контейнера появится **собственный Tailscale IPv4**, который следует записать в клиентский `hub_ip`; IP существующего host daemon для этого профиля не подходит.
