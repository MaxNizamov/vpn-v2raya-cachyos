# Linux Networking 101
## Курс молодого сетевика — то, что нужно чтобы чинить VPN и не только

---

## Модуль 1: Как компьютер видит сеть

### Интерфейсы
Сетевой интерфейс — это точка входа/выхода трафика. Бывают физические (eth0, wlp0s20f3), виртуальные (tun0, docker0), loopback (lo).

```bash
ip link show              # все интерфейсы
ip -br link               # компактно
ip link show wlp0s20f3    # конкретный
```

Состояния: `UP` (включён), `DOWN`, `UNKNOWN`. TUN-интерфейсы почти всегда `UNKNOWN` — это нормально.

### Адреса
```bash
ip addr show              # IP-адреса на интерфейсах
ip -4 addr                # только IPv4
```

Каждый интерфейс может иметь несколько адресов. `scope global` — доступен извне, `scope link` — только в локальной сети, `scope host` — только localhost.

### Практика
```bash
ip link add dummy0 type dummy
ip addr add 10.0.0.1/24 dev dummy0
ip link set dummy0 up
ping 10.0.0.1
ip link del dummy0
```

---

## Модуль 2: Маршрутизация

Таблица маршрутизации говорит ядру: пакет для сети X отправляй через интерфейс Y, шлюз Z.

```bash
ip route show             # основная таблица
ip route show table all   # все таблицы (включая local)
```

### Как читать
```
default via 192.168.1.1 dev wlp0s20f3
         │               └─ через какой интерфейс
         └─ шлюз (gateway)

192.168.1.0/24 dev wlp0s20f3 proto kernel scope link
           └─ сеть            └─ ядро добавило автоматически
```

Правила применяются по принципу «наиболее специфичный маршрут»: `/32` точнее чем `/24`, тот точнее чем `default`.

### Практика
```bash
ip route get 8.8.8.8       # какой маршрут используется для этого IP
ip route add 10.10.0.0/16 via 192.168.1.1
ip route del 10.10.0.0/16
```

---

## Модуль 3: Как работает VPN (TUN-режим)

```
Приложения → система маршрутизации → TUN-интерфейс → VPN-клиент → туннель → сервер
```

TUN работает на уровне IP (L3): все пакеты от приложений направляются в виртуальный интерфейс `tun0` (или `happ-tun`), VPN-клиент их читает, шифрует, отправляет на сервер.

### Ключевые компоненты
1. **TUN-интерфейс** — создаётся VPN-клиентом: `ip tuntap add happ-tun mode tun`
2. **IP-адрес** на TUN: обычно `172.18.0.1/30` (сеть из 2 хостов)
3. **Маршрут по умолчанию** переключается на TUN: `ip route add default via 172.18.0.2 dev happ-tun`
4. **DNS** перенаправляется через TUN для защиты от утечек

### Как проверять
```bash
ip link | grep tun         # создан ли интерфейс?
ip addr show happ-tun      # есть ли адрес?
ip route | grep default    # куда идёт трафик по умолчанию?
```

### Проблемы TUN
| Ошибка | Причина |
|--------|---------|
| `operation not permitted` | Нет прав (нужен root или cap_net_admin) |
| `device or resource busy` | Интерфейс с таким именем уже есть |
| TUN создался, но трафик не идёт | Маршруты не прописались / DNS не работает |

---

## Модуль 4: DNS и почему он ломается при VPN

systemd-resolved, /etc/resolv.conf, NetworkManager — все хотят управлять DNS. При поднятии VPN должно произойти:
1. Старый DNS-сервер заменяется на DNS внутри туннеля
2. Либо все DNS-запросы перехватываются на TUN и проксируются

### Где живёт DNS
```bash
cat /etc/resolv.conf       # что сейчас используется
resolvectl status          # systemd-resolved
nmcli dev show | grep DNS  # DNS от NetworkManager
```

### Типичная проблема
VPN поднялся, TUN создан, маршруты есть, но сайты не открываются — значит DNS-запросы идут мимо туннеля. Решение: заставить систему отправлять DNS через TUN (hijack-dns в sing-box).

### Тест
```bash
dig @172.18.0.2 google.com   # DNS через TUN (должен работать)
dig @8.8.8.8 google.com       # DNS напрямую (утечка, не должен работать при VPN)
```

---

## Модуль 5: Linux Capabilities

Не хотим давать всему root. Capabilities — способ дать процессу конкретное право.

```bash
getcap /path/to/binary     # какие capabilities у файла
setcap cap_net_admin+ep /opt/happ/bin/tun/sing-box
```

### Нужные для TUN
| Capability | Зачем |
|------------|-------|
| `cap_net_admin` | Создание TUN, управление маршрутами, iptables |
| `cap_net_raw` | RAW-сокеты (иногда нужно для ping/traceroute) |

### Что значит +ep
- `+` — добавить
- `e` — effective (активно сейчас)
- `p` — permitted (разрешено использовать)
- `ep` — можно использовать и оно активно

### Проверка у процесса
```bash
cat /proc/<PID>/status | grep -i cap
getpcaps <PID>
```

### Почему это лучше чем root
Процесс может ТОЛЬКО управлять сетью, но не может: удалять файлы, менять пароли, читать /etc/shadow. Принцип минимальных привилегий.

---

## Модуль 6: systemd для сетевых служб

### Unit-файлы
```bash
systemctl cat happd.service     # показать unit
systemctl show happd.service    # все параметры (много!)
```

### Переменные окружения
```
[Service]
Environment="VAR=value"
EnvironmentFile=/etc/sysconfig/happd
```

### Переопределение (override) — не трогаем оригинал
```bash
systemctl edit happd.service    # создаёт /etc/systemd/system/happd.service.d/override.conf
# или вручную:
/etc/systemd/system/happd.service.d/env.conf
```

### Важное
- После изменения unit: `systemctl daemon-reload`
- Рестарт: `systemctl restart happd.service`
- Логи: `journalctl -u happd.service -f`

### Почему /proc/PID/environ может быть пустым
Если процесс вызвал `clearenv()` — файл будет 0 байт. Но переменные из systemd он всё равно получил при старте (проверяется через `systemctl show`).

---

## Модуль 7: Диагностика прав и пользователей

### Кто я?
```bash
whoami                     # текущий пользователь
id                         # группы и UID
ps aux | grep sing-box     # под кем запущен процесс
```

### Может ли пользователь создать TUN?
```bash
# От root: всегда да
# От пользователя: нужен cap_net_admin на бинарнике, либо сам процесс от root
# Проверка:
sudo -u nobody /opt/happ/bin/tun/sing-box run -c config.json
# Если "operation not permitted" — нет прав
```

### polkit / pkexec
```bash
pkexec command             # запуск от root через PolicyKit
# pkexec чистит окружение! Переменные не передаются.
# Используй pkexec env VAR=val command или bash -c 'export VAR=val; command'
```

---

## Модуль 8: Трассировка сетевых проблем

### Пошаговая диагностика
```
1. Есть ли интерфейс?        ip link | grep tun
2. Есть ли IP?               ip addr show tun0
3. Есть ли маршрут?          ip route | grep default
4. Проходит ли ping?         ping -I tun0 8.8.8.8
5. Работает ли DNS?          dig @<dns-ip> google.com
6. Куда идут пакеты?         tcpdump -i tun0 -n
```

### Прослушка портов
```bash
ss -tlnp                   # TCP слушающие порты
ss -ulnp                   # UDP слушающие порты
ss -tlnp | grep 10808      # SOCKS прокси (xray в Happ)
```

### Tcpdump — видим трафик
```bash
tcpdump -i any -n port 53      # весь DNS-трафик
tcpdump -i happ-tun -n          # всё что идёт через TUN
tcpdump -i wlp0s20f3 -n host 8.8.8.8  # трафик к Google DNS
```

### Логи ядра
```bash
dmesg -w                   # смотреть в реальном времени
dmesg | grep -i tun        # проблемы с TUN
dmesg | grep -i drop       # сброшенные пакеты
```

---

## Модуль 9: Happ VPN — архитектура

```
Happ (GUI) → /tmp/happd.sock → happd (systemd, root)
                                  ├─ xray (SOCKS на :10808)
                                  └─ sing-box (TUN: happ-tun)
                                       └─ проксирует трафик в xray
```

### Ключевые файлы
| Путь | Что |
|------|-----|
| `/opt/happ/bin/happd` | Демон |
| `/opt/happ/bin/tun/sing-box` | TUN-драйвер |
| `/opt/happ/bin/core/xray` | Прокси-ядро |
| `~/.config/Happ/config.json` | Конфиг sing-box |
| `~/.config/Happ.conf` | Настройки GUI |
| `/var/log/happd.log` | Лог демона |
| `/tmp/happd.sock` | Сокет для GUI |

### Как чинить
1. **Краш при старте**: `journalctl -u happd.service -f` + нажать Connect
2. **TUN не создаётся**: проверь права sing-box (`getcap`)
3. **TUN создан, трафика нет**: проверь маршруты и DNS
4. **Всё работает но медленно**: mtu, буферы, проверь xray логи

---

## Модуль 10: Шпаргалка команд

```bash
# Интерфейсы
ip link show                              # список
ip link set happ-tun up                   # включить
ip tuntap add happ-tun mode tun           # создать TUN

# Адреса
ip addr add 172.18.0.1/30 dev happ-tun    # назначить IP
ip addr flush dev happ-tun                # сбросить все адреса

# Маршруты
ip route add default via 172.18.0.2       # маршрут по умолчанию
ip route del default                      # удалить
ip route get 8.8.8.8                      # проверить маршрут

# Процессы
ps aux | grep -E 'sing-box|xray|happd'    # кто запущен
cat /proc/PID/status | grep -i cap        # capabilities процесса

# Права
getcap /path/to/binary                    # посмотреть
setcap cap_net_admin+ep /path/to/binary   # дать право

# systemd
systemctl cat unit.service                # unit-файл
systemctl show unit.service | grep Env    # переменные окружения
systemctl restart unit.service            # перезапуск
journalctl -u unit.service -f             # логи

# Сеть
ss -tlnp                                  # слушающие TCP порты
tcpdump -i any -n port 53                 # DNS трафик
ping -I tun0 8.8.8.8                      # пинг через конкретный интерфейс

# DNS
dig google.com                            # запрос
dig @172.18.0.2 google.com                # через конкретный сервер
resolvectl status                         # systemd-resolved
cat /etc/resolv.conf                      # текущий DNS

# Ядро
dmesg | tail -30                          # последние сообщения
modinfo tun                               # инфо о модуле TUN
lsmod | grep tun                          # загружен ли модуль
```

---

## Чек-лист: починить Happ TUN

- [ ] `ENABLE_DEPRECATED_SPECIAL_OUTBOUNDS=true` в systemd
- [ ] `getcap /opt/happ/bin/tun/sing-box` — есть `cap_net_admin`
- [ ] `systemctl restart happd.service`
- [ ] Нажать Connect в GUI
- [ ] `ip link | grep tun` — интерфейс создан
- [ ] `ip route | grep default` — маршрут через TUN
- [ ] `ping 8.8.8.8` — ходит
- [ ] `dig google.com` — резолвится

Если что-то не работает — возвращайся к модулю, который соответствует сломанному шагу.
