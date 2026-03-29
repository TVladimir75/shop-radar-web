"""
Подсказки к полю «товар / тема».
value — латиница для URL на Made-in-China; label — что видит пользователь (RU + EN).
"""

from __future__ import annotations

# (значение для поиска, короткая подсказка по-русски)
_RAW: tuple[tuple[str, str], ...] = (
    ("LED street light", "светильник уличный LED"),
    ("LED panel", "LED-панель, экран"),
    ("LED bulb", "лампочка LED"),
    ("solar street light", "солнечный уличный свет"),
    ("solar panel", "солнечная панель"),
    ("power bank", "повербанк"),
    ("lithium battery", "литиевая батарея"),
    ("electric scooter", "электросамокат"),
    ("Bluetooth speaker", "колонка Bluetooth"),
    ("USB cable", "кабель USB"),
    ("phone case", "чехол для телефона"),
    ("kitchen cabinet", "кухонный шкаф"),
    ("office chair", "офисное кресло"),
    ("plastic injection mold", "литьевая форма, пластик"),
    ("CNC machining", "ЧПУ обработка"),
    ("aluminum profile", "алюминиевый профиль"),
    ("steel pipe", "стальная труба"),
    ("rubber seal", "резиновое уплотнение"),
    ("textile fabric", "ткань"),
    ("garment factory", "швейная фабрика"),
    ("baby stroller", "коляска детская"),
    ("pet products", "товары для животных"),
    ("cosmetic packaging", "упаковка косметики"),
    ("food packaging", "пищевая упаковка"),
    ("electric motor", "электродвигатель"),
    ("water pump", "водяной насос"),
    ("air purifier", "очиститель воздуха"),
    ("industrial fan", "промышленный вентилятор"),
    ("warehouse rack", "стеллаж складской"),
    ("forklift parts", "запчасти погрузчика"),
    ("bearing", "подшипник"),
    ("fastener", "крепёж"),
    ("hand tools", "ручной инструмент"),
    ("garden tool", "садовый инструмент"),
    ("furniture hardware", "мебельная фурнитура"),
    ("ceramic tile", "керамическая плитка"),
    ("floor coating", "покрытие для пола"),
    ("outdoor furniture", "садовая мебель"),
    ("tent", "палатка"),
    ("backpack", "рюкзак"),
    ("sports shoes", "спортивная обувь"),
    ("yoga mat", "коврик для йоги"),
)


def suggest(query: str | None, *, limit: int = 12) -> list[dict[str, str]]:
    """
    Возвращает [{ "value": "...", "label": "..." }, ...]
    value — подставляется в поле и уходит в парсер; label — показ в списке.
    """
    limit = max(1, min(limit, 30))
    if not query or not query.strip():
        return [
            {"value": v, "label": f"{v} · {h}"}
            for v, h in _RAW[:limit]
        ]

    q = query.strip().lower()
    out: list[dict[str, str]] = []
    for value, hint in _RAW:
        v_low = value.lower()
        h_low = hint.lower()
        if (
            q in v_low
            or q in h_low
            or v_low.startswith(q)
            or h_low.startswith(q)
            or any(part.startswith(q) for part in v_low.split())
        ):
            out.append({"value": value, "label": f"{value} · {hint}"})
        if len(out) >= limit:
            break
    if not out:
        # нет совпадений — одна строка «как ввели» для произвольного запроса
        return [{"value": query.strip(), "label": f"Искать: {query.strip()}"}]
    return out[:limit]
