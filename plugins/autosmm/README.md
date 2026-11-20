<div align="center">

# ⚡ AutoSMM

**Плагин автоматической обработки SMM-заказов для FunPay Cardinal**

[![Version](https://img.shields.io/badge/Version-1.0-F59E0B?style=for-the-badge)](./AutoSMM.py)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-203%20passed-22C55E?style=for-the-badge)](#%EF%B8%8F-%D1%81%D0%B1%D0%BE%D1%80%D0%BA%D0%B0-%D0%B8-%D1%82%D0%B5%D1%81%D1%82%D1%8B)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](./LICENSE)

</div>

> **Полный цикл SMM-заказов: запрос ссылки или `@username`, валидация данных покупателя, отправка в PerfectPanel / Peakerr API, трекинг статуса, возвраты и аналитика прямо в Telegram.**

---

## ✨ Основные возможности

- **🔌 Поддержка SMM-панелей** — подключение панелей по API (`PerfectPanel` / `Peakerr`), гибкие API-ключи, проверка баланса и статуса заказов.
- **💬 Диалог с покупателем** — настраиваемые шаблоны ответов, валидация ссылок и `@username`, поддержка отмены и повторного ввода.
- **📦 Лоты и автосинхронизация** — привязка лотов FunPay к сервисам панели, фоновая проверка статусов и автоматический учёт выполнения.
- **💾 Надёжное хранение и бэкапы** — база SQLite (`autosmm.db`), атомарные JSON-конфиги и экспорт/восстановление резервных копий из Telegram.
- **📊 Аналитика и валюты** — расчёт прибыли, конвертация курсов и отчёты по периодам.

---

## 📥 Установка через W1bbex Hub (рекомендуется)

1. Скачайте [W1bbexHub.py](../../W1bbexHub.py?raw=1), поместите в папку `plugins/` FunPay Cardinal и перезапустите Cardinal.
2. В управляющем Telegram-боте отправьте `/hub` и откройте **`🧩 Каталог плагинов`**.
3. Выберите **AutoSMM** и нажмите **«Установить»** (плагин бесплатный, ключ Hub не нужен). При обновлении через Hub старые дубли `AutoSMM` от `@w1bbex` архивируются в `plugins/w1bbex/archive/`.
4. Перезапустите Cardinal (`/restart`). Для управления используйте команду `/autosmm` (доступна также `/chatid`).

---

## 🛠 Установка и сборка из исходников

1. Скачайте репозиторий (**Code → Download ZIP**) и распакуйте архив, либо клонируйте его.
2. Откройте терминал в папке `plugins/autosmm`.
3. Соберите однофайловый плагин:
   - Windows / Linux / macOS: `python build.py`
4. Скопируйте полученный `AutoSMM.py` в папку `plugins/` FunPay Cardinal и перезапустите Cardinal (`/restart`).

> [!IMPORTANT]
> Для работы требуются пакеты из [`requirements.txt`](./requirements.txt): `requests>=2.28.0` и `pyTelegramBotAPI>=4.10.0`. Не загружайте одновременно несколько копий `AutoSMM`.

---

## 📁 Структура плагина

```text
plugins/autosmm/
├── AutoSMM.py                       # Готовый однофайловый бандл (v1.0)
├── build.py                         # Сборщик AutoSMM.py из src/
├── CHANGELOG.md                     # История изменений
├── LICENSE                          # Лицензия MIT
├── README.md                        # Документация плагина
├── requirements.txt                 # Зависимости Python
├── src/
│   ├── runtime.py                   # Метаданные версии (1.0), пути и общие блокировки
│   ├── models.py                    # Модели данных заказов и конфигурации
│   ├── plugin.py                    # Регистрация команд (/autosmm, /chatid) и хуков Cardinal
│   ├── core/                        # Бизнес-логика: buyer_flow, engine, links, orders, panel_api, panel_http, panel_errors, panel_status_parser, refunds
│   ├── services/                    # analytics, currency, notifications, telemetry
│   ├── storage/                     # SQLite (db, db_orders, db_state), config_storage, order_log, migrations, backups, defaults
│   └── tg/                          # Telegram UI и обработчики (lots, lot_editor, panels, settings, callback asmm:)
└── tests/                           # 204 модульных и интеграционных теста
```

---

## ⚙️ Сборка и тесты

```bash
# Пересобрать AutoSMM.py
python build.py

# Запустить тесты AutoSMM
python -m pytest tests -q
```
