"""Правила ToV и бренда из ДЗ 1 — детерминированные проверки, работают до модели-проверяющего."""

import re

from .facts import find_unverified_numbers

# Колонка «Не так» из ToV ДЗ 1 и замечания преподавателя к ДЗ 2
FORBIDDEN_PHRASES = {
    "давление временем": re.compile(r"\b\d+\s*секунд", re.IGNORECASE),
    "обещание оффера": re.compile(r"гарантир\w*|оффер\s+за\s+\d+", re.IGNORECASE),
    "оценка человека": re.compile(r"ваше резюме слабое|вы слабый|неудачник", re.IGNORECASE),
    "навязчивая продажа": re.compile(r"только сегодня|скидк\w*\s*\d+\s*%|!!!", re.IGNORECASE),
    "одобрение компаний": re.compile(r"(?:HR|эйчар|рекрутер)\w*\s+(?:из\s+)?(?:Яндекс|Сбер|Тинькофф|Ozon|VK)\w*", re.IGNORECASE),
    "выдуманная история": re.compile(r"мне показал[аи]?\s+(?:HR|эйчар)|позвали в компанию мечты", re.IGNORECASE),
}

MAX_LEN = {"threads": 500, "tg": 1024, "vk": 2000}   # 1024 — подпись к картинке в Telegram


def check_text(text: str, link_tag: str | None = None, platform: str | None = None) -> list[str]:
    """Возвращает список нарушений. Пустой список — текст прошёл правила."""
    problems = [f"цифра не из опроса: {n}" for n in find_unverified_numbers(text)]
    problems += [f"ToV: {name}" for name, rx in FORBIDDEN_PHRASES.items() if rx.search(text)]
    if link_tag and not re.search(rf"t\.me/\w+\?start={re.escape(link_tag)}\w*", text):
        problems.append(f"нет ссылки на бота с меткой {link_tag}")
    if platform and len(text) > MAX_LEN.get(platform, 10_000):
        problems.append(f"{platform}: длиннее {MAX_LEN[platform]} знаков")
    return problems
