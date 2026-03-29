# Подбор поставщиков (браузер)

Один раз установить зависимости, дальше — запуск и работа **только в браузере** по ссылке.

## Установка (один раз)

```bash
cd shop-radar-web
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Запуск

**Проще всего (macOS):** двойной щелчок по **`Открыть_в_браузере.command`**. Подробности в **`ЗАПУСК.txt`**.

Вручную в терминале:

```bash
cd shop-radar-web
source .venv/bin/activate   # или сразу: .venv/bin/python start.py
python start.py
```

В браузере: **`http://127.0.0.1:8765`** (если вкладка сама не открылась).

Источники: **Made-in-China**, **Alibaba** (витрина), **1688** (может не отвечать вне Китая).

## Важно

Ссылка `127.0.0.1` работает **только на этом компьютере**. Чтобы выложить в интернет с бесплатным поддоменом и HTTPS — см. **`ДЕПЛОЙ.txt`** (например Render → `https://…onrender.com`).
