# Выполняй по шагам: инструкция с обязательной приёмкой

Работай только на новой клиентской VM. Не выполняй следующий шаг, пока критерии текущего не подтверждены фактическим выводом. Команда с exit code 0 без нужных данных не считается доказательством. Этот файл — инструкция, **не отчёт о живой проверке**.

Нельзя менять SSH, firewall, proxy, exit node, теги hub, tailnet policy, выполнять reboot, `tailscale logout`, `tailscale up --reset`, копировать авторизацию или обходить проверки. Нельзя добавлять флаг `--yes`: у bootstrap его нет. Код публичный; рабочие профили, токены и state публиковать нельзя.

## 0. Убедись, что это клиентская VM

Выполни:

```bash
cat /etc/os-release
uname -m
ps -p 1 -o comm=
id
```

**Приёмка:** ОС — Ubuntu 22.04/24.04 либо Debian 12/13; архитектура — `x86_64`/`aarch64`/`arm64`; PID 1 — `systemd`; есть root или рабочий `sudo`. Владелец подтвердил, что это целевая VM, а не согласованный Amsterdam-хаб.

**Стоп:** неподдерживаемая ОС, нет `sudo`/systemd, неизвестно назначение машины. Не изменяй метаданные ОС, чтобы пройти проверку.

## 1. Получи проверенные исходники

Публичный код клонируется без аккаунта GitHub, токена и настроенной авторизации. Проверь наличие Git:

```bash
command -v git
```

Если `git` отсутствует, дождись завершения первичной настройки VM и установи его штатно:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends git ca-certificates
```

После успешной установки:

```bash
git clone https://github.com/djd1m/tailscale-codex-bootstrap.git
cd tailscale-codex-bootstrap
test -f bin/bootstrap
test -f src/tailnet_bootstrap/cli.py
```

Если исходники уже есть, перейди в их каталог вместо повторного `clone`. Если GitHub недоступен, владелец может передать проверенный архив исходников с hub без рабочих профилей и секретов; после распаковки выполни два `test` выше. Токен и GitHub-авторизация для этого репозитория не нужны.

**Приёмка:** оба `test` завершились 0; источник копии известен и разрешён. Здесь и далее команды выполняются из корня этого репозитория.

**Стоп:** apt не завершился успешно, clone не удался, архив содержит рабочие профили, чужие `.codex`, Tailscale state, ключи или неизвестные исполняемые изменения. При apt lock жди владельца блокировки; не удаляй lock-файлы. Проверь доступ к GitHub либо получи чистую копию исходников, не проси GitHub-токен.

## 2. Получи параметры hub, не придумывай их

Проверь вместе с владельцем:

| Параметр | Что подтвердить с владельцем |
| --- | --- |
| Tailscale IP hub | Реальный адрес согласованного hub из `100.64.0.0/10`; `100.64.0.10` — только пример |
| Tailnet | Реальный MagicDNS suffix; `tail000000.ts.net` — только пример |
| Тег App Connector | Согласованный тег hub; `tag:ai-egress-hub` — пример |
| Публичный IPv4 hub | Предоставлен после проверки администратора; это не Tailscale IP и не IP VM |
| Домены | Согласованные точные FQDN, включая `api.ipify.org`; без `*` |
| Не-AI контроль | `checkip.amazonaws.com` отсутствует в списке доменов |

Публичная документация не подтверждает готовность какого-либо рабочего hub. Администратор проверяет тег, роль App Connector, policy и route approvals по [HUB-SETUP](../docs/HUB-SETUP.md). Не выполняй удалённую настройку hub в рамках клиентского bootstrap.

**Приёмка:** подтверждены Tailscale IPv4 hub, MagicDNS suffix, connector tag, публичный IPv4 и список доменов; администратор понимает необходимость App Connector и route approvals. Если hub ещё не подготовлен, после создания правильного профиля разрешено установить программы и сохранить результат `installed`; сетевую настройку останови до готовности hub.

**Стоп:** любой параметр неизвестен, скопирован из примера без подтверждения либо нет подтверждения назначения hub. Шаблон `profiles/pilot.example.json` содержит демонстрационные hub/tailnet/tag и намеренно нерабочий внешний адрес `203.0.113.10`. Не передавай его как рабочий профиль.

## 3. Создай tmux-сессию и рабочий профиль

```bash
sudo bash bin/bootstrap
```

Launcher спросит про недостающие `python3`/`tmux`. Разреши только эту установку, если она согласована владельцем. В мастере `configure` введи проверенные Tailscale IPv4 hub, MagicDNS suffix, connector tag и остальные параметры шага 2; проверь показанный JSON перед сохранением. Рабочий профиль по умолчанию — `/var/lib/tailscale-codex-bootstrap/profile.json`, вне Git. Отдельные `install` и `connect` требуют готовый профиль; wizard создаёт его первым шагом.

**Приёмка профиля:** `version=1`, hub/tailnet/tag совпадают с подтверждёнными владельцем параметрами, а не с демонстрационными значениями из таблицы; `domains` содержит `api.ipify.org`; `probe_domain=api.ipify.org`; `probe_url=https://api.ipify.org`; `non_ai_probe_url=https://checkip.amazonaws.com`; внешний IPv4 совпадает с подтверждённым адресом администратора.

**Стоп:** в JSON неверный адрес, wildcard, секрет или несогласованный домен; локальный Tailscale IP совпадает с `hub_ip` профиля — это hub, а не клиентская VM. Не сохраняй и не продолжай. При проверке существующего профиля не заменяй профиль с активным сетевым baseline; сначала нужен штатный откат старого профиля.

Если SSH оборвался:

```bash
sudo tmux -L ai-egress-bootstrap attach -t node-bootstrap
```

**Приёмка tmux:** отдельный socket `ai-egress-bootstrap`, сессия `node-bootstrap`. Отсоединение — `Ctrl-b`, затем `d`; прокрутка мышью включена, история до 100000 строк. После reboot процесс/сессия/история потеряны: повтори `sudo bash bin/bootstrap`, чтобы продолжить по state. Не обещай восстановление истории терминала.

Если сессия существует, повторный launcher игнорирует новые аргументы и только подключает её. Последующие команды выполняй в shell сессии либо в другом терминале через приведённый ниже `sudo env`. Не запускай вторую операцию, пока первая ещё работает.

## 4. Установи программы и проверь факт установки

В мастере подтверди установку. Для отдельного запуска после выхода мастера:

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli install
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli status
sudo systemctl is-active tailscaled.service
sudo tailscale version
```

Проверь фактический Codex по сохранённому абсолютному пути, без чтения авторизации:

```bash
sudo python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
s = json.loads(Path('/var/lib/tailscale-codex-bootstrap/state.json').read_text())
c = s['installation']['codex']
p = c['path']
assert os.path.isabs(p) and os.access(p, os.X_OK), 'Codex path is not executable'
print('codex_path:', p)
print('package_verified:', c['package_verified'])
subprocess.run([p, '--version'], check=True)
subprocess.run([p, '--help'], check=True)
PY
```

**Приёмка:** зависимости установлены, daemon `active`, `tailscale version` возвращает версию; Codex исполняется и выдаёт ожидаемые version/help. При новой установке ожидаются версия `0.172.0`, `status=installed_official_package`, `package_verified=true`. У сохранённого существующего CLI допустимо `existing_not_reinstalled` и `package_verified=false`; обязательно запиши эту оговорку.

Весь wizard работает от root. Новая установка Codex находится в `/root/.local/bin`, его собственные данные — в root home. Вход выполняет сам владелец в свой аккаунт. Это не перенос аккаунта пользователя SSH и не основание добавлять флаги обхода разрешений Codex.

Проверь права:

```bash
sudo stat -c '%a %U %n' /var/lib/tailscale-codex-bootstrap /var/lib/tailscale-codex-bootstrap/state.json /var/lib/tailscale-codex-bootstrap/profile.json
```

**Приёмка прав:** каталог `700 root`, JSON-файлы `600 root`. В state только несекретные метаданные. Не читай `.codex/auth.json` для проверки.

**Стоп:** installer/checksum/signature error, неактивный daemon, нет Codex или сломан help. Не отключай TLS/signature/checksum проверки. При apt lock дождись cloud-init/apt; не удаляй lock-файлы. Запиши `installed` только для реально завершённых компонентов, а не для всей установки.

## 5. Подключи новый Tailscale-узел

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli connect
sudo tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print({"BackendState":s.get("BackendState"),"SelfID":(s.get("Self") or {}).get("ID"),"CurrentTailnet":s.get("CurrentTailnet"),"MagicDNSSuffix":s.get("MagicDNSSuffix")})'
```

Для нового отключённого узла без идентичности подтверди штатный `tailscale up`; владелец открывает URL и авторизует узел. Ссылки и коды не включай в отчёт.

**Приёмка:** `BackendState=Running`, есть Self ID, MagicDNS suffix в соответствующем поле равен подтверждённому `expected_tailnet` рабочего профиля. Пример `tail000000.ts.net` не используется вместо реальной проверки. Существующая регистрация в правильном tailnet сохраняется.

**Стоп:** другой tailnet, прежняя отключённая идентичность, неуспешный вход или неожиданный reset. Не делай logout/reset/forced reauth. Нужен владелец действующей регистрации.

## 6. Аудит и единственное изменение маршрутизации

До подтверждения ещё раз проверь, что работаешь на клиентской VM, hub подготовлен, а профиль утверждён. Затем:

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli apply
```

Прочитай JSON `route_audit` и `blockers`. Штатный `apply` проверяет узел, hub с нужным тегом, DNS, exit node, proxy и область предлагаемых маршрутов; сохраняет baseline до изменения. После твоего явного подтверждения допускается только `tailscale set --accept-routes=true`. Если приём маршрутов уже включён, bootstrap не должен его переустанавливать.

Проверь выбранные daemon prefs без вывода всех внутренних данных:

```bash
sudo tailscale debug prefs | python3 -c 'import json,sys; p=json.load(sys.stdin); print({k:p.get(k) for k in ("RouteAll","CorpDNS","ExitNodeID","AdvertiseTags")})'
```

**Приёмка:** `blockers=[]`; результат `changed=true` либо `already_enabled=true`; `RouteAll=true`, `CorpDNS=true`, `ExitNodeID` пуст. Baseline сохранён в state; не публикуй его целиком.

**Стоп:** любой блокер, особенно `unexpected_routes`, `connector_not_identified`, `hub_offline`, `refusing_hub_mutation`, `exit_node_active`, `proxy_environment`. Не добавляй ручные маршруты и не удаляй чужие subnet routes. Для hub/политик итог — `NEEDS_HUB_ADMIN`; для прочего запиши конкретную причину. `hub-check` исполняется **на самом hub** и не настраивает его; на клиенте он не является проверкой удалённого hub.

## 7. Выполни свежую сетевую приёмку

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli verify
echo $?
```

Сразу после `verify` `echo $?` должен показать `0`. Прочитай созданный `/var/lib/tailscale-codex-bootstrap/network-report.json` локально; для краткой сводки:

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('/var/lib/tailscale-codex-bootstrap/network-report.json').read_text())
e = r.get('evidence', {})
print({k:r.get(k) for k in ('readiness','checked_at','codex_acceptance','blockers')})
print('control_endpoint_observed_ipv4:', e.get('control_endpoint_observed_ipv4'))
print('non_ai_observed_ipv4:', e.get('non_ai_observed_ipv4'))
print('limitations:', r.get('limitations'))
PY
```

**Приёмка — нужны все условия:**

- `readiness=NETWORK_VERIFIED`, `blockers=[]`, свежее время проверки; `codex_acceptance=NOT_TESTED`.
- Hub IP и тег у одного онлайн-узла в нужном tailnet; exit node и активного proxy нет.
- Для каждого текущего публичного IPv4 выбранного домена: маршрут через `tailscale0`, точный `/32` PrimaryRoute принадлежит нужному hub; нет конкурирующих/посторонних предлагаемых маршрутов.
- IPv6 не даёт проверяемым направлениям доступного/неизвестного обходного пути; IPv6 default отсутствует. Не отключай IPv6 автоматически, если проверка остановилась.
- `control_endpoint_observed_ipv4` равен проверенному публичному IPv4 hub; контроль `checkip.amazonaws.com` маршрутизируется с обычного VM egress и видит другой IPv4.

**Стоп:** exit code не 0, любой блокер, отсутствующее evidence или противоречивый IP. `status` показывает сохранённый прогресс и не заменяет свежий `verify`. Пустой список блокеров в старом отчёте не подтверждает текущую сеть. Независимый echo доказывает только IP на этом echo, а не IP, увиденный OpenAI.

## 8. Вход и реальный пилот Codex

Только после успешного шага 7:

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli login
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli launch
```

Владелец сам завершает `codex login --device-auth` своей учётной записью. Затем в запущенном Codex выполните обычный реальный запрос и дождитесь полного ответа, включая завершение streaming. Не добавляй `--dangerously-bypass-approvals-and-sandbox` или аналогичные флаги.

Для вспомогательного наблюдения активных HTTPS-соединений в другом терминале:

```bash
sudo ss -Hntp state established '( dport = :443 )'
```

Сопоставляй **реальные** endpoint-ы OAuth/device polling, inference и streaming с утверждённым инвентарём, DNS и маршрутами. Для каждого обнаруженного FQDN выполни `getent ahostsv4 ТОЧНЫЙ_FQDN`; для каждого возвращённого IPv4 — `ip -j route get ТОЧНЫЙ_IP`. Проверяй принадлежность `/32` hub по свежему сетевому отчёту. Записывай имена, адреса, время и результат, без содержимого запросов, токенов, кодов и файлов авторизации. Команда `ss` сама по себе не раскрывает hostname и не доказывает полноту инвентаря.

**Приёмка `CODEX_VERIFIED` возможна только вручную:** собственный вход завершён; реальный запрос получил успешный полный ответ; фактически наблюдённые endpoint-ы покрыты утверждённой маршрутизацией; указан способ и время наблюдения. Независимый echo при этом не превращается в измерение IP на стороне OpenAI.

**Стоп / итог `MANUAL_VERIFICATION_REQUIRED`:** нужен владелец для device auth; реальный запрос не выполнен/неуспешен; endpoint-ы нельзя наблюдать или надёжно сопоставить; появился домен вне профиля; есть IPv6/DoH/другой неучтённый путь. Сохрани `NETWORK_VERIFIED` только как результат своего снимка, Codex пометь «не принят». Не выводи `CODEX_VERIFIED` из login exit code, `ss`, версии, help или автотестов. CLI сам не выдаёт `CODEX_VERIFIED`.

## 9. Откат при необходимости

Из корня репозитория:

```bash
sudo env PYTHONPATH="$PWD/src" python3 -m tailnet_bootstrap.cli rollback
sudo tailscale debug prefs | python3 -c 'import json,sys; print("RouteAll:",json.load(sys.stdin).get("RouteAll"))'
```

**Приёмка:** нет блокеров; `changed=true` либо `already_restored=true`; `RouteAll` совпадает с исходным baseline. Откат касается только изменения `accept-routes` данным bootstrap, не удаляет пакеты и авторизацию.

**Стоп:** иной профиль, изменившаяся идентичность или несовпавшая настройка. Не форсируй откат вручную и не удаляй state. Передай владельцу точный блокер для разбора внешних изменений.

## 10. Итоговый отчёт

Заполни по фактам, не оставляя подразумеваемой «готовности»:

```text
VM / ОС / архитектура / время UTC:
Профиль и источник проверенного публичного IPv4 hub:
installed: да / частично / нет; версии Tailscale и Codex:
Codex package_verified: true / false; новая / сохранённая установка:
NETWORK_VERIFIED: да / нет; время verify; блокеры:
IP независимого echo / IP не-AI контроля:
CODEX_VERIFIED: да (ручная приёмка) / нет / не выполнялось:
Реальный запрос и наблюдения endpoint-ов: выполнено / не выполнено:
Откат: не требовался / выполнен / блокирован:
Следующее действие владельца / администратора:
```

Нельзя писать «живой тест пройден», если выполнялись только тесты репозитория. Нельзя обещать строгий fail-closed: маршруты App Connector действуют по IP для всех процессов, общие CDN затрагивают другие домены, а будущие DNS-ответы и исчезновение маршрутов могут вернуть прямой выход.
