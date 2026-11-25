# Packet flow

## Обычный HTTPS через подготовленный маршрут

```mermaid
sequenceDiagram
    autonumber
    participant C as Codex CLI на VM
    participant D as Системный DNS и discovery
    participant W as Tailscale VM
    participant H as App Connector Amsterdam
    participant N as Forwarding и SNAT
    participant P as HTTPS провайдера
    C->>D: Разрешить выбранный FQDN
    D-->>W: Текущие A и AAAA, изучение маршрута
    Note over W,H: Маршрут должен быть одобрен и выбрать этот хаб
    C->>W: TCP SYN к IP провайдера через tailscale0
    W->>H: Зашифрованный IP-пакет через direct или relay
    H->>N: Расшифрованный внутренний IP-пакет
    N->>P: Пакет с публичным исходящим IP хаба
    P-->>N: Ответ HTTPS
    N-->>H: Conntrack обратного пути
    H-->>W: Ответ внутри Tailscale
    W-->>C: Ответ TCP и сквозной TLS
    Note over C,P: Хаб не расшифровывает содержимое TLS
```

Codex видит обычный HTTPS и системный DNS. Смена IP происходит на сетевом уровне хаба (SNAT), а не в TLS. Внутренний адрес VM автоматически в заголовок HTTPS не добавляется. У провайдера могут быть дополнительные признаки учётной записи и клиента; контрольный IP echo не даёт доступа к его внутренним журналам.

## Отказ и контроль

```mermaid
sequenceDiagram
    participant B as Bootstrap verify
    participant K as Ядро VM
    participant H as Connector
    participant E as Независимый HTTPS echo
    B->>K: Проверить все выбранные A и AAAA
    alt Все IPv4 через нужный connector, пригодного IPv6 нет
        B->>E: Новый HTTPS с закреплённым проверенным A
        E-->>B: Наблюдаемый публичный IPv4
        B->>B: Сверить IP хаба и обычный non-AI маршрут
    else Нужного маршрута нет или найден обход
        B->>B: NETWORK_NOT_VERIFIED или NEEDS_HUB_ADMIN
        Note over B,H: Успешная установка не является приёмкой
    end
    Note over K,H: После исчезновения маршрута возможен прямой выход VM
```

Проверка закреплённого A устанавливает путь к конкретному адресу. Дополнительный обычный запрос без закрепления и реальный Codex нужны, чтобы проверить участие системного resolver и текущее множество доменов приложения. Отсутствие прямого Tailscale-соединения не обязательно означает отказ: возможен DERP/peer relay с другой задержкой.

Обычный non-AI endpoint должен использовать прямой интернет VM. Исчезновение маршрута, DNS/DoH в приложении и новые CDN-адреса рассматриваются отдельно; текущий пилот не устанавливает firewall kill switch.
