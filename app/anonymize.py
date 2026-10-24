"""Обезличивание на нашем сервере, без ИИ. В модель уходит только уже обезличенный текст.

Слой 1 — правила: телефоны, почта, ссылки, ники, юрлица, известные компании, ФИО.
Слой 2 — Natasha: локальное распознавание имён, организаций и мест в русском тексте
(модели входят в пакет и работают без интернета, данные никуда не отправляются).
Контроль — `find_leftovers` прогоняет оба слоя ещё раз; если что-то нашлось, кейс
останавливается и до модели не доходит.
"""

import re
from functools import lru_cache

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

NER_LABELS = {"PER": ("имя", "[имя]"), "ORG": ("организация", "[компания]"), "LOC": ("место", "[город]")}
# Площадки и сервисы, которые не указывают на человека: их не маскируем
NOT_IDENTIFYING = {"vk", "вк", "вконтакте", "telegram", "телеграм", "телеграме", "hh", "hh.ru", "headhunter",
                   "instagram", "инстаграм", "threads", "youtube", "ютуб", "дзен", "google", "excel", "figma"}


def anonymize_rules(text: str) -> tuple[str, list[str]]:
    """Слой 1: заменяет найденное правилами на метки. Возвращает текст и типы находок."""
    found: list[str] = []
    for label, pattern, replacement in RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            found.append(label)
    return text, found


@lru_cache(maxsize=1)
def _natasha():
    from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter
    return Doc, Segmenter(), NewsNERTagger(NewsEmbedding())


def _ner_spans(text: str):
    Doc, segmenter, tagger = _natasha()
    doc = Doc(text)
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    # метки вида [имя] модель NER иногда помечает сама — их пропускаем
    return [s for s in doc.spans if s.type in NER_LABELS and not s.text.startswith("[")
            and s.text.strip(" .,").lower() not in NOT_IDENTIFYING]


def anonymize_ner(text: str) -> tuple[str, list[str]]:
    """Слой 2: имена, организации и места через локальную модель Natasha."""
    spans = _ner_spans(text)
    found = sorted({NER_LABELS[s.type][0] for s in spans})
    for s in sorted(spans, key=lambda s: s.start, reverse=True):
        text = text[:s.start] + NER_LABELS[s.type][1] + text[s.stop:]
    return text, found


def anonymize(text: str) -> tuple[str, list[str]]:
    """Оба слоя подряд. Вызывается до любого обращения к модели."""
    text, found_rules = anonymize_rules(text)
    text, found_ner = anonymize_ner(text)
    return text, found_rules + found_ner


def find_leftovers(text: str) -> list[str]:
    """Контроль после маскировки: правила и NER ещё раз. Непустой список = стоп."""
    left = [label for label, pattern, _ in RULES if pattern.search(text)]
    left += sorted({NER_LABELS[s.type][0] for s in _ner_spans(text)})
    return left
