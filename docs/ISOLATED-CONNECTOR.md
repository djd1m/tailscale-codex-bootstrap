# Изолированный App Connector на существующем хабе

Вариант для администратора, которому нужно сохранить пользовательскую идентичность Tailscale основного Amsterdam-хаба. [Шаблон](../hub/compose.yaml) запускает отдельный daemon в отдельном Docker bridge и регистрирует новую tagged-ноду `ai-egress-amsterdam`. Это план ручного развёртывания; наличие файлов в Git не означает, что контейнер, маршруты, центральная policy или реальный egress проверены.

## До запуска

Сохраните работающее SSH-соединение и независимый путь восстановления через консоль провайдера. Проверьте исходную идентичность хостового Tailscale, доступ к SSH и существующим приложениям. Не меняйте теги, настройки, состояние или сокет хостового daemon. Команды `tailscale` для новой ноды ниже всегда выполняются через `docker compose exec connector` с собственным `--socket`.

Администратор хоста обследует `ip -4 route show table all`, `ip -6 route show table all`, `docker network ls` и IPAM каждого существующего Docker network. Новая bridge-подсеть должна быть частной, свободной и не пересекаться с LAN/VPN/tailnet и используемыми контейнерными сетями. Не используйте диапазон Tailscale `100.64.0.0/10`. Проверьте наличие `/dev/net/tun`, Linux Docker Engine и Compose v2, доступ к официальному image, исходящие DNS/HTTPS/UDP и возможность DERP fallback. Ресурсный лимит 512 MiB нужно подтвердить наблюдением под пилотной нагрузкой.

Снимите локальный baseline: `docker ps`, `docker network inspect` существующих сетей, маршруты, `iptables-save` и/или `nft list ruleset`, состояние host Tailscale и проверки существующих сервисов. Сохраните вывод в root-owned каталог `0700`, файлы `0600`, вне Git. Туда же сохраните экспорт центральной tailnet policy, прежние route approvals и точное описание планируемого добавления. Снимки могут содержать инфраструктурные данные; не публикуйте их целиком. Не выполняйте firewall flush, restart Docker или остановку действующих Compose-проектов.

Изоляция сохраняет отдельный network namespace и идентичность, но не отделяет ядро. Docker создаёт bridge/NAT/forwarding правила на хосте и может затронуть его forwarding; до и после запуска нужно сравнить baseline и проверить прежние приложения. Контейнерный IPv4 forwarding задан через namespaced sysctl, а не через изменение host sysctl. См. [Docker bridge networking](https://docs.docker.com/engine/network/drivers/bridge/) и [правила Docker с iptables](https://docs.docker.com/engine/network/firewall-iptables/).

## Центральная policy — обязательный отдельный шаг

Owner/Admin/Network admin сначала объединяет узкий пилот с действующей policy и проверяет валидатором Tailscale. Шаблон Docker не даёт таких полномочий и не меняет policy автоматически. Для `tag:ai-egress-hub` должен быть определён `tagOwners`; назначать тег должен уполномоченный владелец. Пустой список владельцев в примере оставляет назначение администратору. Подготовьте policy **до** browser login с `--advertise-tags`.

Начните с одного согласованного точного FQDN IP-echo и его актуальных публичных A-адресов. В клиентском профиле репозитория это `api.ipify.org`; другие домены добавляются только после проверки. Объедините [демонстрационный фрагмент](../examples/tailnet-policy.fragment.json) с действующей policy по [инструкции администратора](HUB-SETUP.md), заменив условные домен и префиксы проверенными значениями. Пилот не должен создавать wildcard-домены, `autoApprovers.routes` для `0.0.0.0/0` или `::/0`, grants к `autogroup:internet` или доступ всех пользователей ко всему интернету. Одобрение маршрутов ограничьте конкретными IPv4 `/32` или согласованными точными префиксами; новые DNS-адреса требуют пересмотра.

Центральная конфигурация должна связать точный домен с connector tag через `nodeAttrs.app`, допустить pilot-клиента к discovery на connector через нужные `tcp:53`/`udp:53` и разрешить только согласованный HTTPS к проверенным префиксам через этот connector (`via`). Действующие широкие ACL/grants продолжают действовать: узкий фрагмент их не отменяет. В IPv4-пилоте не одобряйте IPv6-маршруты. На pilot-клиенте отдельно подтвердите отсутствие пригодного публичного IPv6-пути; выключенный IPv6 forwarding контейнера не блокирует прямой IPv6 клиента. Роли, теги и discovery описаны в [официальной инструкции App Connector](https://tailscale.com/docs/features/app-connectors/how-to/setup), ограничения пути — в [grants via](https://tailscale.com/docs/features/access-control/grants/grants-via).

## Подготовить отдельное состояние

Следующие команды предназначены **только для первой установки** администратором на проверенном целевом хосте из корня доверенной копии исходников. Не запускайте клиентский bootstrap на хабе ради создания контейнера. Сначала убедитесь, что Compose project `ai-egress-amsterdam` ещё не занят. Блок ниже отказывается работать при существующих deployment-каталоге или state, проверяет родительские каталоги и не перезаписывает файлы. При отказе обследуйте найденные данные; не выбирайте другой путь, чтобы обойти проверку действующей установки.

```bash
sudo python3 - "$PWD" <<'PY'
import os, stat, sys
from pathlib import Path

os.umask(0o077)
repo = Path(sys.argv[1])
deploy = Path('/root/ai-egress-amsterdam')
state = Path('/opt/tailscale-ai-egress/state')
sources = [(repo / 'hub/compose.yaml', deploy / 'compose.yaml'),
           (repo / 'hub/connector.env.example', deploy / 'connector.env')]
for source, _ in sources:
    if source.is_symlink() or not source.is_file():
        sys.exit(f'Остановлено: исходник должен быть обычным файлом: {source}')
for target in (deploy, state):
    if os.path.lexists(target):
        sys.exit(f'Остановлено: путь уже существует, нужен разбор установки: {target}')
for parent in (Path('/root'), Path('/opt'), state.parent):
    if os.path.lexists(parent):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            sys.exit(f'Остановлено: небезопасный родительский каталог: {parent}')
    elif parent != state.parent:
        sys.exit(f'Остановлено: отсутствует ожидаемый каталог: {parent}')
if not state.parent.exists():
    state.parent.mkdir(mode=0o700)
deploy.mkdir(mode=0o700)
state.mkdir(mode=0o700)
for source, target in sources:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(source.read_bytes())
print('Новые deployment/state созданы; файлы конфигурации не перезаписывались.')
PY
```

Только после успешного выполнения блока отредактируйте новый env:

```bash
sudoedit /root/ai-egress-amsterdam/connector.env
```

В env задайте проверенный `TAILSCALE_IMAGE`, свободный `CONNECTOR_IPV4_SUBNET`, абсолютный `CONNECTOR_STATE_DIR` и два независимых IPv4 DNS upstream: `CONNECTOR_DNS_IPV4_PRIMARY` и `CONNECTOR_DNS_IPV4_SECONDARY`. DNS defaults отсутствуют: администратор явно выбирает доступные из namespace контейнера resolver-адреса. Например, `1.1.1.1` и `9.9.9.9` подходят только после проверки доступности и соответствия требованиям сети. Не используйте MagicDNS `100.100.100.100`, другие tailnet-адреса, loopback или `127.0.0.11` в качестве upstream. Выбранные resolver не должны пересылать app-домены обратно в tailnet или этот connector. При изменении пути state заранее создайте именно этот новый root-owned каталог `0700`. Bind mount использует `create_host_path: false`, чтобы опечатка не создавала новый каталог молча. В нём будут секретные ключи новой Tailscale-ноды: каталог `0700`, state-файлы `0600`. Команда запуска задаёт `umask 077`; права существующих файлов проверьте отдельно. Не монтируйте `/var/lib/tailscale`, `/run/tailscale` или иные данные хостового daemon. Резервную копию нового state храните отдельно с теми же ограничениями и не запускайте одновременно два daemon с одной идентичностью.

Явный `dns` применяется только к connector-контейнеру. Без него Docker может унаследовать host resolver, использующий MagicDNS: после добавления app-домена его DNS-запрос вернётся в тот же App Connector и образует цикл. `--accept-dns=false` у контейнерного Tailscale не исправляет унаследованный Docker upstream. В пользовательском Docker bridge внутри `/etc/resolv.conf` может по-прежнему отображаться embedded DNS `127.0.0.11`; проверяйте заданные upstream через `HostConfig.Dns` и реальный DNS-запрос. Не меняйте host resolver или DNS существующих контейнеров. См. [Docker DNS services](https://docs.docker.com/engine/network/#dns-services).

`TAILSCALE_IMAGE` обязателен и должен иметь вид `tailscale/tailscale@sha256:<64 hex>`. Администратор выбирает реально опубликованный официальный release, проверяет manifest и записывает digest; версия host Tailscale не гарантирует наличие одноимённого Docker tag. Пустое значение останавливает Compose. Не подставляйте плавающие `latest`/`stable` или просто tag вместо digest. Статический `config` проверяет интерполяцию, но не подлинность или формат выбранного image; администратор отдельно проверяет immutable reference до `pull`. `NET_ADMIN` и `NET_RAW` нужны для kernel networking; `privileged` и `SYS_MODULE` отсутствуют. Если TUN или требуемая поддержка kernel недоступны, остановитесь, а не расширяйте привилегии или настройки работающего хоста. См. [Docker configuration Tailscale](https://tailscale.com/docs/features/containers/docker/docker-params) и [официальный образ](https://hub.docker.com/r/tailscale/tailscale).

## Проверить конфигурацию и запустить только daemon

Для краткости команды дальше используют shell-функцию. Она всегда указывает отдельные проект и внешние файлы; вызов самой функции ничего не развёртывает:

```bash
connector_compose() {
  sudo docker compose --project-name ai-egress-amsterdam \
    --env-file /root/ai-egress-amsterdam/connector.env \
    -f /root/ai-egress-amsterdam/compose.yaml "$@"
}
connector_validate_image() {
  connector_compose config --format json | python3 -c '
import json, re, sys
image = json.load(sys.stdin)["services"]["connector"]["image"]
if not re.fullmatch(r"tailscale/tailscale@sha256:[0-9a-f]{64}", image):
    sys.exit("Expected official tailscale/tailscale image pinned by sha256 digest")
print("Official repository and immutable image reference: OK")
'
}
connector_validate_image
```

Для **повторного развёртывания или обновления** первичный блок не запускают:

1. Сверьте существующие Compose project, image, node ID, state path и закрытые deployment-файлы. Согласуйте перерыв pilot-трафика; сохраните SSH и контроль действующих приложений хаба.
2. Остановите только этот connector командой `connector_compose stop connector`, предварительно убедившись, что функция указывает на его действующие файлы. Остальные контейнеры, Docker Engine и host Tailscale продолжают работать.
3. Пока daemon остановлен, создайте согласованную резервную копию его **фактического** state и deployment-файлов в отдельном root-owned каталоге `0700`, файлы `0600`, вне Git. Проверьте возможность чтения копии и SHA-256; state и env могут содержать секреты, их содержимое в отчёт не выводят.
4. Сравните новый шаблон с текущими файлами, сохраняя согласованные image, DNS, CIDR и абсолютный state path. Подготовьте проверенную версию конфигурации в **новом** защищённом deployment-каталоге с запретом перезаписи и symlink, затем явно поменяйте пути `--env-file` и `-f` в функции. Существующий state не заменяйте новым и не копируйте в параллельно запускаемый daemon.
5. Выполните `config`, проверку immutable image, запуск только `connector` и всю приёмку ниже; подтвердите прежние node ID и Tailscale IP. Возврат старого image на текущем state допустим только при заранее проверенной совместимости. Иначе остановите только connector, сохраните неудачный state и отдельно согласуйте восстановление offline-копии. Не запускайте старый daemon на изменённом state без этой проверки.

После проверки baseline и свободного CIDR администратор создаёт только новый сервис:

```bash
connector_validate_image &&
  connector_compose pull connector &&
  connector_compose up -d --no-deps connector
connector_compose ps connector
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock status
```

На новом пустом state ожидается `NeedsLogin`/`Logged out`, а не готовность маршрутов. Запущен непосредственно `tailscaled`; [wrapper containerboot](https://github.com/tailscale/tailscale/blob/v1.104.7/cmd/containerboot/main.go), выполняющий `tailscale up`, обойдён. Поэтому `TS_EXTRA_ARGS` здесь не добавляет тег и роль. Путь state/socket и kernel TUN передаются демону явно; [daemon CLI](https://tailscale.com/docs/reference/tailscaled) описывает эти параметры. Перезапуск после успешного личного входа использует сохранённую идентичность. Сравните маршруты и netfilter с baseline и повторите проверки прежних сервисов **до** регистрации новой ноды.

До включения app-домена проверьте DNS из namespace контейнера. Команды ниже выводят только настроенные DNS-адреса и выполняют прямой запрос к каждому upstream; для этого в image должен быть доступен `nslookup`:

```bash
connector_check_dns() {
  local connector_id resolver resolver_list
  connector_id=$(connector_compose ps --quiet connector) || return 1
  test -n "$connector_id" || return 1
  sudo docker inspect --format '{{json .HostConfig.Dns}}' "$connector_id" || return 1
  resolver_list=$(connector_compose config --format json | python3 -c '
import json, sys
print("\n".join(json.load(sys.stdin)["services"]["connector"]["dns"]))
') || return 1
  for resolver in $resolver_list; do
    connector_compose exec -T connector nslookup api.ipify.org "$resolver" || return 1
  done
}
connector_check_dns
```

Оба upstream должны вернуть актуальные A-адреса. Это проверяет DNS, но не одобрение маршрутов или egress через connector. Если `nslookup` отсутствует, используйте проверенный DNS-инструмент в том же network namespace и явно укажите upstream; не подменяйте проверку запросом через host resolver.

Для исправления DNS уже зарегистрированного connector сначала сохраните его state, node ID и Tailscale IP, остановите pilot-трафик и временно уберите только добавленную app-domain mapping, если она создаёт цикл. Сохраните тот же `CONNECTOR_STATE_DIR`, image и остальные параметры в приватных deployment-файлах; добавьте в них явные DNS upstream и секцию `dns` из актуального шаблона. После `connector_validate_image` пересоздайте только этот сервис:

```bash
connector_compose up -d --no-deps --force-recreate connector
connector_compose ps connector
connector_check_dns
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock status
```

Подтвердите прежние node ID, tag и Tailscale IP, доступ к control plane и отсутствие DNS timeout/rate-limit в новых логах; затем повторите проверки прежних host-сервисов. State не удаляйте, host daemon и Docker Engine не перезапускайте. Возвращайте только ранее согласованную domain mapping после проверки DNS; route approvals остаются отдельным шагом.

## Личный вход и отдельное включение роли

Когда центральные `tagOwners` и узкая policy готовы, владелец лично выполняет:

```bash
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock login \
  --hostname=ai-egress-amsterdam \
  --advertise-tags=tag:ai-egress-hub \
  --accept-dns=false --accept-routes=false
```

Владелец открывает появившуюся ссылку в собственном браузере и выбирает нужный tailnet. Если назначение тега отклонено, остановитесь и исправьте центральные права; не меняйте идентичность основного хаба. Не используйте auth keys/OAuth secrets, чужую сессию браузера или копию авторизации. Ссылка входа, журналы с ней и данные сессии не должны попасть в Git, публичный отчёт или передачу третьим лицам.

После подтверждения новой ноды и тега в admin console включите роль отдельно, без сброса других параметров:

```bash
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock set \
  --advertise-connector=true
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock ip -4
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock status --json
```

Запишите **новый** Tailscale IPv4 контейнера, tag, имя и node ID в приватную приёмку. У основного хаба останется другой Tailscale IP и прежняя пользовательская идентичность. В отдельном локальном профиле pilot VM задайте `hub_ip` новой ноды, согласованные `expected_tailnet`, `connector_tag`, точные `domains` и измеренный `expected_egress_ipv4`. Не используйте host Tailscale IP как адрес connector.

## Проверки и границы результата

Внутри namespace проверьте IPv4 forwarding `1`, IPv6 forwarding `0`, интерфейсы, маршруты и штатный SNAT Tailscale. На хосте подтвердите отдельный bridge без IPv6 и отсутствие новых published ports, host network, host sockets/state mount. Проверьте установленный image и ресурсные ограничения контейнера. Настройки sysctl и `status` сами по себе не подтверждают forwarding, разрешённую policy или внешний IP.

Из network namespace контейнера измерьте публичный IPv4 независимым HTTPS IP-echo. Например, если установлен host `nsenter` и `curl`, после проверки, что сервис существует и его PID положительный:

```bash
connector_egress_ipv4() {
  local connector_id connector_pid
  connector_id=$(connector_compose ps --quiet connector) || return 1
  test -n "$connector_id" || return 1
  connector_pid=$(sudo docker inspect --format '{{.State.Pid}}' "$connector_id") || return 1
  test "$connector_pid" -gt 0 || return 1
  sudo nsenter --target "$connector_pid" --net -- \
    curl --disable --fail --silent --show-error --ipv4 --noproxy '*' \
    --proto '=https' --connect-timeout 5 --max-time 15 https://api.ipify.org
}
connector_egress_ipv4
```

Host curl использует container network namespace, но host resolver-файлы. Если host DNS listener недоступен оттуда, используйте следующий отдельный способ. Сначала получите и сверяйте актуальную публичную A-запись **именно `api.ipify.org`** через ранее проверенные независимые DNS из namespace контейнера (`connector_check_dns`). Запишите один подтверждённый адрес в переменную `CONNECTOR_PROBE_IPV4` текущего shell; не используйте адрес из непроверенного сообщения или случайный CDN IP. Функция ниже остановится при пустом, непубличном или не-IPv4 значении:

```bash
connector_egress_ipv4_verified_a() {
  local connector_id connector_pid
  : "${CONNECTOR_PROBE_IPV4:?Задайте проверенную текущую публичную A-запись api.ipify.org}"
  python3 - "$CONNECTOR_PROBE_IPV4" <<'PY'
import ipaddress, sys
try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    sys.exit('Нужен проверенный публичный IPv4 api.ipify.org')
if address.version != 4 or not address.is_global or address.is_multicast:
    sys.exit('Нужен проверенный публичный IPv4 api.ipify.org')
PY
  test "$?" -eq 0 || return 1
  connector_id=$(connector_compose ps --quiet connector) || return 1
  test -n "$connector_id" || return 1
  connector_pid=$(sudo docker inspect --format '{{.State.Pid}}' "$connector_id") || return 1
  test "$connector_pid" -gt 0 || return 1
  sudo nsenter --target "$connector_pid" --net -- \
    curl --disable --fail --silent --show-error --ipv4 --noproxy '*' \
    --proto '=https' --connect-timeout 5 --max-time 15 \
    --resolve "api.ipify.org:443:$CONNECTOR_PROBE_IPV4" https://api.ipify.org
}
connector_egress_ipv4_verified_a
```

`--resolve` обходит resolver только для этого запроса: hostname URL, SNI и обычная проверка TLS-сертификата остаются `api.ipify.org`; `-k` не используется. Настройки resolver хоста и контейнеров не меняются. Этот запрос измеряет container egress, но сам по себе не проверяет штатный DNS-путь: DNS проверяют отдельно, а после его восстановления повторяют обычный HTTPS без `--resolve`. Измерение самого host egress не заменяет проверку контейнера. Полученный адрес занесите в профиль; совпадение адресов ожидается при обычном Docker MASQUERADE, но должно быть измерено.

С отдельной pilot VM пройдите [матрицу приёмки](ACCEPTANCE.md): текущие A-адреса точного домена через нужный connector, одобренные маршруты и `PrimaryRoutes`, HTTPS echo совпадает с измеренным container egress, независимый non-AI endpoint идёт напрямую с VM. Адреса CDN/DNS меняются: новые префиксы не одобряются автоматически вне согласованного списка. Отдельно требуются собственный Codex login и реальный запрос с наблюдением OAuth/inference/streaming. Для первичного echo-пилота успешный Codex не заявляется.

App Connector маршрутизирует IP, поэтому общий CDN-адрес может затронуть другие домены. При исчезновении маршрутов возможен прямой выход клиента; строгий fail-closed этот шаблон не создаёт. IPv6 bridge выключен и IPv6 forwarding контейнера `0`, однако это не отключает IPv6 основного хаба или pilot VM. IPv4-приёмка блокируется при обнаружении пригодного публичного IPv6-пути VM. Свой Tailscale IPv6 адрес ноды не равен публичному IPv6 egress.

## Откат только нового сервиса

Сначала остановите pilot-трафик и восстановите клиентский `accept-routes` штатным `rollback`. Администратор tailnet сверяет сохранённый baseline и отменяет **только** добавленные для пилота app/grants/route approvals. Существующую policy целиком не заменяйте. При доступном daemon можно сначала выключить его новую роль:

```bash
connector_compose exec connector \
  tailscale --socket=/run/tailscale/tailscaled.sock set \
  --advertise-connector=false
connector_compose stop connector
connector_compose rm --force connector
```

State остаётся в `CONNECTOR_STATE_DIR`. До удаления новой сети убедитесь через `docker network inspect ai-egress-amsterdam_connector`, что она создана этим проектом, имеет выбранный IPAM и больше не содержит endpoints. Затем удалите **только её**:

```bash
sudo docker network rm ai-egress-amsterdam_connector
```

Не выполняйте `docker compose down` для чужого проекта, `down -v`, `docker system prune`, удаление state или общий сброс netfilter. Повторно сравните host Tailscale, SSH, приложения, сети и маршруты с baseline. Не восстанавливайте снимок iptables/nftables вслепую поверх работающего Docker. Удаление новой ноды из admin console и уничтожение её секретного state — отдельные осознанные действия владельца; обычный откат сохраняет state для восстановления той же ноды.
