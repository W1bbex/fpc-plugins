<div align="center">

# 🧩 W1bbex FPC Plugins

**Экосистема модульных плагинов и единый менеджер обновлений для FunPay Cardinal**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-FunPay%20Cardinal-2AABEE?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/w1bbexplugins)
[![Hub](https://img.shields.io/badge/W1bbex%20Hub-v0.5.8-22C55E?style=for-the-badge)](./plugins/w1bbex-hub/README.md)
[![AutoSMM](https://img.shields.io/badge/AutoSMM-v1.0-F59E0B?style=for-the-badge)](./plugins/autosmm/README.md)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](./LICENSE)

</div>

> **Открытый репозиторий исходников и готовых однофайловых сборок плагинов от `@W1bbex`: установка и обновление из Telegram через W1bbex Hub без ручной возни с файлами.**

---

## 📦 Состав репозитория

| Плагин | Версия | Команды | Доступ | Назначение |
| :--- | :---: | :--- | :---: | :--- |
| **[W1bbex Hub](./plugins/w1bbex-hub/README.md)** | `0.5.8` | `/hub` | Бесплатный | Менеджер каталога, лицензий, установки и автообновления плагинов |
| **[AutoSMM](./plugins/autosmm/README.md)** | `1.0` | `/autosmm`, `/chatid` | Бесплатный | Автоматическая обработка и трекинг SMM-заказов через PerfectPanel / Peakerr API |

---

## 🚀 Быстрый старт (через W1bbex Hub)

Рекомендуемый способ — установить один раз **[`W1bbexHub.py`](./W1bbexHub.py?raw=1)** и управлять остальными плагинами прямо из Telegram:

1. Скачайте установочный файл [**W1bbexHub.py**](./W1bbexHub.py?raw=1) из корня репозитория.
2. Поместите `W1bbexHub.py` в папку `plugins/` вашего FunPay Cardinal.
3. Перезапустите Cardinal (`/restart`).
4. Отправьте `/hub` в управляющем Telegram-боте и нажмите **«Каталог плагинов»**.
5. Выберите нужный плагин (например, **AutoSMM**), нажмите **«Установить»** и выполните `/restart`.

> [!TIP]
> Платные плагины поддерживаются через **«Добавить ключ»** в `/hub`, а бесплатные ставятся в один клик без ключа.

---

## ✨ Возможности

### 👨‍💻 W1bbex Hub (`v0.5.8`)
- **Каталог прямо в Telegram** — установка, переустановка, обновление и удаление плагинов без SSH и FTP.
- **Уведомления об обновлениях Hub и плагинов** — фоновая проверка самого Hub и плагинов каждые 30 минут с переключателем **«Уведомления включены / выключены»**.
- **Безопасная установка** — проверка SHA-256 и синтаксиса `.py`, автоматическая архивация legacy-дублей и генерация loader-файлов.
- **Самообновление Hub** — загрузка новой версии в `W1bbexHub.py` и баннер-подтверждение после перезапуска Cardinal.

### ⚡ AutoSMM (`v1.0`)
- **Привязка лотов FunPay** — гибкий маппинг товаров на услуги подключённых SMM-панелей (PerfectPanel / Peakerr API).
- **Диалог с покупателем** — запрос ссылки или `@username`, валидация ввода, настраиваемые шаблоны сообщений и отмена заказа.
- **Надёжное хранение** — SQLite (`autosmm.db`) и атомарная запись конфигов с резервным копированием и восстановлением из Telegram.
- **Финансы и аналитика** — учёт себестоимости, конвертация валют, проверка балансов панелей и отчёты по заказам.

---

## 📁 Структура репозитория

```text
fpc-plugins/
├── W1bbexHub.py                  # Готовый однофайловый бандл W1bbex Hub (v0.5.8)
├── LICENSE                       # Лицензия MIT
├── README.md                     # Общая документация репозитория
└── plugins/
    ├── w1bbex-hub/               # Исходники и сборщик W1bbex Hub
    │   ├── build.py              # Генератор корневого W1bbexHub.py
    │   ├── README.md             # Документация W1bbex Hub
    │   ├── src/                  # runtime, storage, core, ui/, handlers, plugin
    │   └── tests/                # Модульный и интеграционный pytest-набор Hub
    └── autosmm/                  # Исходники, сборщик и бандл AutoSMM
        ├── AutoSMM.py            # Готовый однофайловый бандл AutoSMM (v1.0)
        ├── build.py              # Генератор plugins/autosmm/AutoSMM.py
        ├── README.md             # Документация AutoSMM
        ├── CHANGELOG.md          # История версий
        ├── LICENSE               # Лицензия MIT
        ├── requirements.txt      # Зависимости (requests, pyTelegramBotAPI)
        ├── src/                  # runtime, models, plugin, core/, services/, storage/, tg/
        └── tests/                # Модульный и интеграционный pytest-набор AutoSMM
```

---

## ⚙️ Сборка из исходников и тестирование

Плагины разрабатываются модульно в `src/`, а для установки в FunPay Cardinal собираются в независимые однофайловые `.py`-бандлы.

```bash
# Сборка W1bbexHub.py (результат — в корне репозитория)
python plugins/w1bbex-hub/build.py

# Сборка AutoSMM.py (результат — plugins/autosmm/AutoSMM.py)
python plugins/autosmm/build.py
```

Запуск проверок выполняется отдельно из папки каждого плагина, чтобы изолировать пакеты `src/`:

```bash
# Проверки W1bbex Hub
cd plugins/w1bbex-hub && python -m pytest tests -q

# Проверки AutoSMM
cd plugins/autosmm && python -m pytest tests -q
```

---

## 🤝 Поддержка и ссылки

- 📢 **Telegram-канал с обновлениями**: [@w1bbexplugins](https://t.me/w1bbexplugins)
- 💬 **Связь с автором**: [@w1bbex](https://t.me/w1bbex)
- 💻 **GitHub**: [W1bbex](https://github.com/W1bbex)

---

## 📜 Лицензия

Проект распространяется под лицензией [MIT](./LICENSE).
