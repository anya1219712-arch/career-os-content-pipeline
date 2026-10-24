"""Обезличивание, слой 1: правила. Работает до модели, поэтому в модель не уходят контакты.

Слой 2 (модель) убирает то, что правилами не поймать: имена, редкие должности, детали проектов.
После обоих слоёв `find_leftovers` ещё раз ищет следы — если нашлись, кейс останавливается.
"""

import re

PHONE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:ru|com|рф|io|net|org|me|su|by|kz|pro|info|site)\b(?:/[^\s,;]*)?",
                 re.IGNORECASE)
HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")
# «ООО «Ромашка»», «АО Альфа», «ПАО Сбербанк»
LEGAL_ENTITY = re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ИП|LLC|Ltd|Inc)\b\.?\s*[«\"']?[^,.;\n»\"']{2,40}[»\"']?")
# ФИО: «Иванов Иван Иванович» или «Иван Иванов»
FULL_NAME = re.compile(r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ий|ая|ова|ева|ина|ына|ский|ская)\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична))?\b")

# Крупные компании, которые часто встречаются в резюме. Список пополняем.
KNOWN_COMPANIES = [
    "Яндекс", "Сбер", "Сбербанк", "Тинькофф", "Т-Банк", "Альфа-Банк", "ВТБ", "Ozon", "Озон",
    "Wildberries", "Вайлдберриз", "МТС", "Мегафон", "Билайн", "Авито",
    "Касперский", "Ростелеком", "Газпром", "Лукойл", "X5", "Магнит", "ВкусВилл", "Самокат",
]
_COMPANY = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in KNOWN_COMPANIES) + r")\w*", re.IGNORECASE)

RULES = [
    ("телефон", PHONE, "[телефон]"),
    ("почта", EMAIL, "[почта]"),
    ("ссылка", URL, "[ссылка]"),
    ("ник", HANDLE, "[ник]"),
    ("юрлицо", LEGAL_ENTITY, "[компания]"),
    ("компания", _COMPANY, "[компания]"),
    ("ФИО", FULL_NAME, "[имя]"),
]


def anonymize_rules(text: str) -> tuple[str, list[str]]:
    """Заменяет персональные данные на метки. Возвращает текст и список найденных типов."""
    found: list[str] = []
    for label, pattern, replacement in RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            found.append(label)
    return text, found


def find_leftovers(text: str) -> list[str]:
    """Финальная проверка: остались ли следы персональных данных после двух слоёв."""
    return [label for label, pattern, _ in RULES if pattern.search(text)]
