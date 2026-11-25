# Диагностика

Работайте из корня репозитория и остановитесь на первом неразрешённом блокере. Не удаляйте state: там исходное значение для отката. Не публикуйте auth keys, URL/коды входа, `.codex/auth.json`, полный `tailscale debug prefs` или весь каталог состояния.

Базовые команды:

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli status
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli verify
sudo systemctl is-active tailscaled.service
```

`status` — сохранённый прогресс. Свежую готовность показывает `verify`: JSON, exit code и `/var/lib/tailscale-codex-bootstrap/network-report.json`. Exit code 0 требуется вместе с `NETWORK_VERIFIED` и отсутствием блокеров.

## SSH, tmux, повторный запуск

**SSH оборвался:** `sudo tmux -L ai-egress-bootstrap attach -t node-bootstrap`. Нужны тот же root socket и имя. Обычный `tmux attach` может показать другой сервер. `Ctrl-b`, затем `d` только отсоединяет клиента. Мышь и история 100000 строк действуют в запущенной сессии.

**Сессии нет:** `sudo bash bin/bootstrap` создаст её заново. После reboot старого процесса, истории и сессии нет; продолжаются сохранённые этапы, а не процесс в памяти.

**Переданный `verify` не выполнился:** существующая сессия имеет приоритет, новые аргументы launcher игнорируются. В shell сессии запустите `PYTHONPATH=src python3 -m tailnet_bootstrap.cli verify` либо используйте базовую команду выше в отдельном терминале после завершения текущей операции.

**«Другой процесс настройки уже работает»:** подключитесь к его tmux. Не удаляйте lock-файлы и не запускайте параллельный мастер в другом state-каталоге.

## Пакеты и Codex CLI

**apt занят:** дождитесь cloud-init, unattended-upgrades или другого apt. Проверьте `cloud-init status` (если установлен), `ps -ef` и `sudo dpkg --audit`. Не удаляйте `/var/lib/dpkg/lock*`; повторите установку после завершения владельца блокировки. Если dpkg сообщает незавершённую настройку, её восстановление согласуйте с оператором VM.

**Ошибка DNS/HTTPS:** проверьте `getent ahostsv4 pkgs.tailscale.com`, время `date -u`, обычный сетевой доступ и Cloud.ru policy. Не обходите TLS, проверку подписи или контрольную сумму. Установочные upstream-ы должны быть доступны до настройки selected-domain routing.

**Checksum mismatch / изменённый apt source:** сохраните точную безопасную ошибку, остановитесь и получите проверенное обновление репозитория. Не исправляйте hash под скачанный файл, не отключайте `signed-by` и не перезаписывайте существующие чужие source/key файлы. Установщик намеренно сохраняет конфликтующую конфигурацию.

**tailscaled не active:** локально выполните `sudo journalctl -u tailscaled --no-pager -n 80`; изучите вывод на VM, перед пересылкой удалите URL/коды и идентификаторы, которые не нужны для разбора. Повторный запуск не требует сброса идентичности.

**Codex есть, но shell его не находит:** wizard работает от root; новая установка обычно `/root/.local/bin/codex`, состояние покажет фактический `installation.codex.path`. Проверяйте `--version` и `--help` по этому пути. Не копируйте root авторизацию другому пользователю. Существующий CLI сохраняется; `package_verified=false` означает, что комплектность полного пакета не подтверждена, даже при рабочем help.

## Вход подтверждён, первая карта сети не приходит

Симптом: браузер подтвердил вход, в локальном журнале есть `machineAuthorized=true; authURL=false`, но `tailscale up` продолжает ждать, а каждые две минуты появляются `map response long-poll timed out!` и `PollNetMap: ... context canceled`. Это уже этап получения карты сети. В [исходниках v1.104.7](https://github.com/tailscale/tailscale/blob/v1.104.7/control/controlclient/direct.go) двухминутный watchdog сам отменяет запрос без ответа; `context canceled` здесь не доказывает отмену входа пользователем. Успешный короткий `whoami` ещё не проверяет поток карты.

Все следующие действия выполняйте **только на настраиваемой VM через её консоль**, сохраняя state и настройки hub. [`Ctrl-C` у ожидающего `tailscale up`](https://github.com/tailscale/tailscale/blob/v1.104.7/cmd/tailscale/cli/up.go) завершает ожидание CLI, daemon продолжает работу. Сначала проверьте альтернативный транспорт без изменения daemon:

```bash
sudo env TS_FORCE_NOISE_443=true timeout 45s tailscale debug ts2021 --verbose
```

Если Noise handshake и `whoami` через TCP 443 не проходят, остановитесь на этой ошибке. Если проходят, можно проверить временный обход ниже. `TS_FORCE_NOISE_443` — внутренний диагностический env-knob, проверенный в [v1.104.7](https://github.com/tailscale/tailscale/blob/v1.104.7/control/controlhttp/client.go): он отключает TCP 80 для control connection. Для другой версии проверьте её исходники. Успех не гарантирован и сам по себе не устанавливает причину сбоя.

Создайте собственный runtime drop-in и **один раз** перезапустите daemon. Блок проверяет, что оба пути свободны; не изменяйте эти файлы параллельно с выполнением команд:

```bash
sudo bash <<'SH'
set -eu
runtime=/run/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf
persistent=/etc/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf
test ! -e "$runtime"
test ! -L "$runtime"
test ! -e "$persistent"
test ! -L "$persistent"
install -d /run/systemd/system/tailscaled.service.d
printf '[Service]\nEnvironment="TS_FORCE_NOISE_443=true"\n' > "$runtime"
systemctl daemon-reload
systemctl restart tailscaled
SH
```

Если первая регистрация ещё не сохранилась, restart может потребовать обычного входа заново: один раз выполните `sudo tailscale up --accept-routes=false --timeout=60s` и завершите вход в браузере. Не пересылайте URL. Не используйте logout/reset, удаление state или чтение ключей.

```bash
sudo tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(json.dumps({k:s.get(k) for k in ("BackendState","TailscaleIPs")},indent=2))'
sudo tailscale ip -4
```

Требуются `Running` и назначенный Tailscale IP. Повторите проверку **через три минуты**: соединение должно сохраняться дольше 120 секунд, а в локальном журнале не должен повторяться цикл map timeout. Дополнительно проверьте online в админ-консоли или `connectedToControl` через уже доступный API; проверьте связь с нужным peer. При неуспехе сохраните безопасную ошибку и выполните точечный откат ниже, не повторяйте регистрацию по кругу.

Только после такой проверки закрепите **тот же известный файл**: блок сверяет его содержимое, копирует в `/etc`, убирает runtime-копию и перечитывает unit **без второго restart**. До этого runtime-настройка исчезнет при reboot.

```bash
sudo bash <<'SH'
set -eu
runtime=/run/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf
persistent=/etc/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf
printf '[Service]\nEnvironment="TS_FORCE_NOISE_443=true"\n' | cmp -s - "$runtime"
test ! -e "$persistent"
test ! -L "$persistent"
install -D -m 0644 "$runtime" "$persistent"
rm -- "$runtime"
systemctl daemon-reload
SH
```

Откат касается только двух файлов, созданных этим примером. Блок отклоняет символические ссылки, проверяет тип и содержимое существующих файлов. Если обоих файлов уже нет, перезапуска не будет. Не изменяйте их параллельно с откатом; другие drop-in и state он не удаляет:

```bash
sudo bash <<'SH'
set -eu
files=(/run/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf
       /etc/systemd/system/tailscaled.service.d/90-ai-egress-control443.conf)
verified=false
for file in "${files[@]}"; do
  test ! -L "$file"
  if test -e "$file"; then
    test -f "$file"
    printf '[Service]\nEnvironment="TS_FORCE_NOISE_443=true"\n' | cmp -s - "$file"
    verified=true
  fi
done
if test "$verified" = false; then
  exit 0
fi
rm -f -- "${files[@]}"
systemctl daemon-reload
systemctl restart tailscaled
SH
```

## Tailnet, hub и маршруты

| Блокер / симптом | Что проверить | Следующий шаг |
| --- | --- | --- |
| `tailscale_not_running`, прежняя регистрация | `sudo tailscale status` локально; существует ли нужная идентичность | Владелец возобновляет её; автоматического logout/reset нет |
| `wrong_tailnet` | MagicDNS suffix должен совпадать с `expected_tailnet` рабочего профиля | Владелец согласует регистрацию; не заменять её принудительно |
| `connector_not_identified` | Один узел с `hub_ip` и `connector_tag` рабочего профиля | Администратор hub/tailnet, [HUB-SETUP](HUB-SETUP.md) |
| `hub_offline` | Hub online в status; доступ владельца к нему | Администратор восстанавливает hub без клиентских обходов |
| `primary_route_wrong_connector`, `competing_connector` | Точный `/32` и принадлежность PrimaryRoute нужному hub | Администратор проверяет discovery, approval и конкурирующие connectors |
| `unexpected_routes` | `route_audit`, PrimaryRoutes и Offered/Allowed routes | Администратор разбирает весь предлагаемый scope; не игнорировать маршруты как «не относящиеся к Codex» |
| `route_acceptance_disabled` | Выбранный `RouteAll` и сохранённый baseline | После аудита и явного согласия штатный `apply` |
| `tailscale_dns_disabled` | Выбранный `CorpDNS`; как VM получает DNS | Отдельное согласованное решение владельца; bootstrap DNS не меняет |
| `exit_node_active` | Выбранный exit node уже меняет весь egress | Владелец разбирает его назначение; bootstrap его не отключает |
| `proxy_environment` | Только имена proxy-переменных в evidence, без значений | Владелец разбирает proxy; не печатать весь env и не удалять настройки молча |
| `dns_unresolved` | `getent ahostsv4 ТОЧНЫЙ_FQDN` | Исправить DNS/профиль согласованно; не подставлять статический IP из интернета |
| `route_not_tailscale` | `ip -j route get ТОЧНЫЙ_IP` и PrimaryRoute | Проверить DNS discovery/approvals/accept-routes; не добавлять route вручную |
| `ipv6_egress_available`, `ipv6_requires_separate_acceptance` | `ip -6 -j route show default`, AAAA и пути из evidence | Отдельная приёмка IPv6; не объявлять IPv4-проверку достаточной |
| `probe_failed`, `egress_mismatch` | Probe DNS, маршруты, HTTP 200, ожидаемый публичный IPv4 | Не угадывать IP и не исключать echo из профиля ради прохождения |
| `non_ai_probe_failed`, `non_ai_route_tailscale`, `non_ai_egress_matches_hub` | `checkip.amazonaws.com` вне domains, нормальный egress VM | Разобрать прямой контроль/общие IP; выборочность пока не принята |

Готовность hub проверяется отдельно: публичная документация не содержит результатов проверки рабочего развёртывания. Пока тег, роль App Connector, policy и approvals не подтверждены, `NEEDS_HUB_ADMIN` требует действий администратора. `hub-check` проверяет локальную роль **на самом hub**, не переключает роль и не настраивает удалённую машину. Его запуск на новой VM не является доказательством состояния согласованного Amsterdam-хаба.

`100.64.0.10`, `tail000000.ts.net`, `tag:ai-egress-hub` в примерах — демонстрационные параметры. Создайте профиль через `configure` с подтверждёнными владельцем значениями до `install`/`connect`. Проверка совпадения локального Tailscale IP с `hub_ip` блокирует клиентскую установку на hub; не обходите её заменой IP в профиле.

`accept-routes=true` относится к предлагаемым маршрутам tailnet в целом. Поэтому обход `unexpected_routes` опасен для смысла этой конфигурации: успешный echo мог бы оказаться следствием постороннего широкого маршрута.

## Сеть проверена, Codex не работает

`NETWORK_VERIFIED` подтверждает текущий снимок выбранных DNS/маршрутов и независимый echo. Не подтверждает успешный OAuth, inference или IP, увиденный OpenAI.

После сетевой проверки используйте штатные `login` и `launch`. `login` вызывает `codex login --device-auth`; владелец входит в свой аккаунт. Если требуется личное действие, остановите этот шаг до его выполнения, сохранив установленную систему. Не переносите чужую `.codex` и не просите прислать код/токен.

При неуспешном реальном запросе выясните, какой **фактический** endpoint использовал клиент и попал ли он в согласованный инвентарь. Снимок `sudo ss -Hntp state established '( dport = :443 )'` помогает увидеть подключения, но сам не раскрывает имена и не подтверждает полноту. Недостающие endpoint-ы требуют согласованных изменений hub и профиля, повторного аудита и `verify`. Без таких наблюдений оставьте `MANUAL_VERIFICATION_REQUIRED`.

Успешный login или `--version` не дают `CODEX_VERIFIED`. Нельзя заменять неуспешный пилот запросом curl к другому endpoint-у и объявлять Codex проверенным.

## Откат заблокирован

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli rollback
```

Rollback восстанавливает только сохранённый `accept-routes`, проверяя исходный профиль, идентичность и последнее ожидаемое значение. При `profile_mismatch`, `identity_changed`, `preference_changed` или повреждённом baseline не форсируйте `tailscale set` и не удаляйте state. Администратор должен разобрать внешние изменения. Если настройка уже совпадает с исходной, `already_restored` — нормальный результат.

После отката Codex, Tailscale и собственная авторизация остаются установленными. Деавторизация и удаление пакетов — отдельные действия владельца, не часть этой команды.

## Что передать для разбора

Достаточно версии bootstrap, ОС/архитектуры, времени UTC, безопасного кода ошибки, `readiness`, списка `blockers`, выбранных version/package полей и краткой маршрутной сводки. Адреса и FQDN передавайте только в разрешённом приватном контексте. Не пересылайте весь env, полный daemon prefs, авторизацию, device codes или state архивом. Пишите отдельно, какие шаги выполнены: установка, сетевой снимок, собственный вход и реальный пилот.
