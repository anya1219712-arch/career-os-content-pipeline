"""Белый список фактов: только реальные цифры опроса 10 практиков найма (ДЗ 1, октябрь 2026).

Модель может ссылаться только на эти цифры. Любая другая цифра вида «N из 10» или процент
останавливает выпуск — это детерминированная проверка, она не зависит от модели.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    key: str
    count: int          # сколько из 10
    text: str           # формулировка для выпуска


SURVEY_FACTS: list[Fact] = [
    Fact("result_in_numbers", 10, "10 из 10 рекрутеров ждут в резюме масштаб и измеримый результат"),
    Fact("functions_vs_vacancy", 9, "9 из 10 рекрутеров первым делом сверяют функции с вакансией"),
    Fact("short_tenure", 8, "8 из 10 рекрутеров настораживает меньше года на одном месте"),
    Fact("format_filters", 6, "6 из 10 рекрутеров отсеивают по формату, должности и фразе-фильтру"),
]

ALLOWED_COUNTS = {f.count for f in SURVEY_FACTS}

# «7 из 10», «7 рекрутеров из 10», «семь из десяти»
_N_OF_10 = re.compile(r"\b(\d{1,2})\s*(?:\S+\s+)?из\s*10\b", re.IGNORECASE)
_WORD_OF_10 = re.compile(
    r"\b(один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+(?:\S+\s+)?из\s+десяти\b",
    re.IGNORECASE,
)
_WORDS = {"один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
          "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s*%")


def find_unverified_numbers(text: str) -> list[str]:
    """Возвращает цифры опроса, которых нет в белом списке, и любые проценты."""
    problems: list[str] = []
    for m in _N_OF_10.finditer(text):
        if int(m.group(1)) not in ALLOWED_COUNTS:
            problems.append(m.group(0))
    for m in _WORD_OF_10.finditer(text):
        if _WORDS[m.group(1).lower()] not in ALLOWED_COUNTS:
            problems.append(m.group(0))
    problems += [m.group(0) for m in _PERCENT.finditer(text)]
    return problems


def facts_for_prompt() -> str:
    return "\n".join(f"- [{f.key}] {f.text}" for f in SURVEY_FACTS)


def fact_by_key(key: str) -> Fact | None:
    return next((f for f in SURVEY_FACTS if f.key == key), None)
