"""Простой рейтинг карточки (чем выше — «лучше» для пользователя)."""


def score_row(
    *,
    has_audited: bool,
    business_type: str | None,
    has_region: bool,
    has_employees: bool,
    name_len: int,
) -> float:
    s = 0.0
    if has_audited:
        s += 25
    bt = (business_type or "").lower()
    if "manufacturer" in bt or "factory" in bt:
        s += 30
    elif "trading" in bt:
        s += 5
    else:
        s += 10
    if has_region:
        s += 15
    if has_employees:
        s += 10
    s += min(20, name_len / 5)
    return round(s, 1)


def score_to_stars(score: float) -> int:
    """Оценка карточки в звёздах 1–5 (для отображения; сортировка по числу score_row)."""
    if score <= 0:
        return 1
    n = int(score / 20.0 + 0.5)
    return max(1, min(5, n))
