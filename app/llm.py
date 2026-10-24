"""Клиент моделей. Selectel Token Factory работает по OpenAI-совместимому API,
поэтому используем библиотеку openai и меняем только base_url и ключ.

Если основной провайдер не ответил, пробуем резервный (Cloud.ru или Yandex AI Studio).
Без ключа включается заглушка MockLLM: конвейер, бот и тесты работают без затрат на токены.
"""

import json
import re
from pathlib import Path

from .config import settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def parse_json(text: str) -> dict:
    """Модели иногда оборачивают JSON в ```json ... ```, достаём его аккуратно."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Модель вернула не JSON: {text[:200]}")
    return json.loads(match.group(0))


class LLM:
    def __init__(self):
        from openai import OpenAI  # импорт здесь, чтобы заглушка работала и без библиотеки
        self.main = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60, max_retries=1)
        self.fallback = None
        if settings.llm_fallback_base_url:
            self.fallback = OpenAI(base_url=settings.llm_fallback_base_url,
                                   api_key=settings.llm_fallback_api_key, timeout=60, max_retries=1)

    def ask(self, task: str, model: str, system: str, user: str) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for client in filter(None, [self.main, self.fallback]):
            try:
                resp = client.chat.completions.create(model=model, messages=messages, temperature=0.4)
                return parse_json(resp.choices[0].message.content or "")
            except Exception as e:  # сеть, лимиты, неверный JSON — пробуем резерв
                last_error = e
        raise RuntimeError(f"Модель недоступна на шаге {task}: {last_error}")


class MockLLM:
    """Детерминированная заглушка: повторяет логику шагов без вызова модели."""

    def ask(self, task: str, model: str, system: str, user: str) -> dict:
        data = json.loads(user)
        if task == "filter":
            ok = len(data.get("resume", "")) > 20 and len(data.get("story", "")) > 20
            return {"fits": ok, "reason": "есть разрыв между сделанным и написанным" if ok else "мало данных"}
        if task == "draft_molodets":
            # Как сделала бы модель: в «что написал» берём только строки опыта, без контактов
            parts = [p.strip() for p in data["resume"].split(".") if p.strip()]
            duties = [p for p in parts if "[" not in p and "контакт" not in p.lower()]
            return {
                "hook": "Молодец, надо переписать: разбираем резюме, где сделано больше, чем написано.",
                "did": data["story"].strip().rstrip(".") + ".",
                "wrote": ". ".join(duties) + ".",
                "recruiter_view": "Рекрутер видит обязанности и не видит результата, поэтому отклик выглядит как у всех.",
                "fix": "Перепиши строку через результат: что изменилось после тебя и в каких цифрах.",
                "fact_key": "result_in_numbers",
            }
        if task == "draft_threads_reply":
            return {
                "reply": "Посмотрела резюме. Первое, что заметит рекрутер: в опыте перечислены обязанности, "
                         "а результата не видно. Попробуй переписать верхние две строки через то, что изменилось после тебя.",
                "fact_key": "result_in_numbers",
            }
        if task == "draft_hr_eyes":
            return {
                "title": "Что рекрутер ищет в опыте первым",
                "body": "Он сверяет, совпадают ли твои функции с вакансией, и только потом читает детали. "
                        "Поэтому верхние строки опыта стоит подогнать под требования.",
                "fact_key": "functions_vs_vacancy",
            }
        if task == "adapt":
            # Threads: заход, исправление и факт — одна мысль, короче 450 знаков
            paragraphs = [p for p in data["tg"].split("\n\n") if p.strip()]
            lines = [l for l in data["tg"].split("\n") if l.startswith("✏️")]
            fix = lines[0].replace("✏️ Как исправить: ", "") if lines else ""
            threads = "\n\n".join(p for p in [paragraphs[0], fix, paragraphs[-1]] if p)
            return {"threads": threads[:450], "vk": data["tg"]}
        if task == "storyboard":
            d, angle = data["draft"], data["angle"]
            if angle == "tip":
                scenes = [{"kind": "hook", "caption": d.get("title", "")[:70], "voice": d.get("title", "")},
                          {"kind": "tip", "caption": "Верхние строки опыта — под требования вакансии",
                           "voice": "Подгони первые строки опыта под требования вакансии."}]
            else:
                wrote = ", ".join(d.get("wrote", "").split(".")[0].split(", ")[:2])
                did = d.get("did", "").split(",")[0][:65]
                first = {"expectation": {"kind": "did", "caption": did, "voice": "Вот что человек сделал на работе."},
                         "recruiter": {"kind": "wrote", "caption": f"«{wrote}»", "voice": "А вот что он написал в резюме."},
                         "fix": {"kind": "wrote", "caption": f"Было: «{wrote}»", "voice": "Было так."}}[angle]
                second = {"expectation": {"kind": "wrote", "caption": f"«{wrote}»", "voice": "А в резюме — одна сухая строка."},
                          "recruiter": {"kind": "recruiter", "caption": "Видны обязанности, результата нет",
                                        "voice": "Рекрутер видит обязанности и не видит результата."},
                          "fix": {"kind": "fix", "caption": "Стало: что изменилось после тебя",
                                  "voice": "Перепиши строку через результат: что изменилось после тебя."}}[angle]
                scenes = [{"kind": "hook", "caption": "Опыт есть, его просто не видно",
                           "voice": "Опыт есть, его просто не видно."}, first, second]
            scenes.append({"kind": "outro", "caption": "Молодец, надо переписать",
                           "voice": "Проверь своё резюме в боте Career OS."})
            return {"scenes": scenes, "fact_key": "result_in_numbers"}
        if task == "review":
            return {"ok": True, "issues": []}
        raise ValueError(f"Неизвестный шаг: {task}")


def get_llm():
    return MockLLM() if settings.use_mock_llm else LLM()
