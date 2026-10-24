"""Конвейер: одна механика для разных рубрик. Меняются вход и промпт, шаги и проверки те же.

Шаги: согласие → обезличивание (правила + модель) → отбор → черновик → версии под площадки →
проверка (правила, затем вторая модель) → одна доработка → автор → редактор.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import anonymize, reels, rules
from .card import render_card
from .config import settings
from .db import DB, iso_week
from .facts import facts_for_prompt, fact_by_key
from .llm import get_llm, load_prompt


@dataclass(frozen=True)
class Rubric:
    key: str
    prompt: str
    platforms: tuple[str, ...]
    link_tag: str
    weekly_limit: int
    needs_consent: bool = False     # кейс от пользователя: нужно согласие и подтверждение автора
    uses_filter: bool = False
    counts_as_post: bool = True     # входит в лимит 5 постов в неделю из аудита ДЗ 2


RUBRICS = {
    "molodets": Rubric("molodets", "draft_molodets", ("tg", "threads", "vk"), "molodets",
                            weekly_limit=settings.special_per_week, needs_consent=True, uses_filter=True),
    "hr_eyes": Rubric("hr_eyes", "draft_hr_eyes", ("tg", "threads", "vk"), "hr_eyes", weekly_limit=4),
    # Г3: до двух ответов в будни; ответы не считаются постами канала
    "threads_reply": Rubric("threads_reply", "draft_threads_reply", ("threads",), "threads_reply",
                            weekly_limit=10, counts_as_post=False),
}


@dataclass
class Result:
    case_id: int
    status: str
    draft_id: int | None = None
    texts: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    card_path: str | None = None


def bot_link(rubric: Rubric, platform: str) -> str:
    tag = f"{rubric.link_tag}_{datetime.now():%Y%m%d}" if rubric.key == "threads_reply" else f"{rubric.link_tag}_{platform}"
    return f"https://t.me/{settings.bot_username}?start={tag}"


def compose(rubric: Rubric, draft: dict) -> str:
    """Собираем пост из полей модели. Цифру опроса подставляет программа из белого списка."""
    fact = fact_by_key(draft.get("fact_key", ""))
    fact_line = f"{fact.text} — по нашему опросу практиков найма." if fact else ""
    if rubric.key == "molodets":
        body = (f"{draft['hook']}\n\n"
                f"🛠 Что сделал: {draft['did']}\n"
                f"📝 Что написал: «{draft['wrote']}»\n"
                f"👀 Как прочитают на отборе: {draft['recruiter_view']}\n"
                f"✏️ Как исправить: {draft['fix']}")
    elif rubric.key == "hr_eyes":
        body = f"{draft['title']}\n\n{draft['body']}"
    else:
        body = draft["reply"]
    return "\n\n".join(p for p in [body, fact_line] if p)


class Pipeline:
    def __init__(self, db: DB, llm=None):
        self.db = db
        self.llm = llm or get_llm()

    def _ask(self, case_id: int, task: str, model: str, prompt: str, payload: dict) -> dict:
        system = load_prompt(prompt)
        result = self.llm.ask(task, model, system, json.dumps(payload, ensure_ascii=False))
        self.db.log(case_id, task, model)
        return result

    def _stop(self, case_id: int, status: str, note: str) -> Result:
        self.db.update_case(case_id, status=status, note=note)
        self.db.log(case_id, status, note)
        return Result(case_id, status, problems=[note])

    def run(self, case_id: int) -> Result:
        case = self.db.get_case(case_id)
        rubric = RUBRICS[case["rubric"]]

        # 1. Согласие
        if rubric.needs_consent and not case["consent_at"]:
            return self._stop(case_id, "stopped_no_consent", "нет согласия на разбор")

        # 2. Обезличивание на нашем сервере, без ИИ: правила + локальная Natasha.
        #    Модель видит только результат этого шага; если контроль нашёл следы — стоп до модели.
        resume, _ = anonymize.anonymize(case["raw_input"] or "")
        story, _ = anonymize.anonymize(case["real_story"] or "")
        leftovers = sorted(set(anonymize.find_leftovers(resume) + anonymize.find_leftovers(story)))
        if leftovers:
            return self._stop(case_id, "stopped_pii", "после обезличивания остались: " + ", ".join(leftovers))
        self.db.log(case_id, "anonymized_locally")
        self.db.update_case(case_id, anonymized=json.dumps({"resume": resume, "story": story}, ensure_ascii=False),
                            status="anonymized")

        # 3. Отбор кейса под сегмент
        if rubric.uses_filter:
            verdict = self._ask(case_id, "filter", settings.model_cheap, "filter", {"resume": resume, "story": story})
            if not verdict.get("fits"):
                return self._stop(case_id, "rejected_filter", verdict.get("reason", "не подходит"))

        # 4–6. Черновик → версии → проверки, с одной доработкой
        payload = {"resume": resume, "story": story, "facts": facts_for_prompt()}
        if rubric.key == "hr_eyes":
            payload = {"topic": resume, "facts": facts_for_prompt()}
        if rubric.key == "threads_reply":
            payload = {"post": resume, "facts": facts_for_prompt()}

        texts, problems, draft = {}, [], {}
        for attempt in range(2):
            draft = self._ask(case_id, f"draft_{rubric.key}", settings.model_draft, rubric.prompt, payload)
            texts = self._build_texts(case_id, rubric, draft)
            problems = self._check(case_id, rubric, texts, source={"resume": resume, "story": story})
            if not problems or attempt == 1:
                break
            payload = {**payload, "fix_these_problems": problems}   # одна доработка с замечаниями
            self.db.log(case_id, "redraft", "; ".join(problems))

        status = "checks_failed" if problems else ("waiting_author" if rubric.needs_consent else "waiting_editor")
        draft_id = self.db.add_draft(case_id, texts, problems, status, settings.model_draft,
                                     settings.model_review, draft.get("fact_key", ""), draft_json=draft)
        card_path = None
        if "tg" in texts and rubric.key == "molodets":
            card_path = str(render_card(draft_id, draft, fact_by_key(draft.get("fact_key", ""))))
            self.db.update_draft(draft_id, card_path=card_path)
        self.db.update_case(case_id, status="drafted")
        self.db.log(case_id, "drafted", status)
        return Result(case_id, status, draft_id, texts, problems, card_path)

    def _build_texts(self, case_id: int, rubric: Rubric, draft: dict) -> dict:
        main = compose(rubric, draft)
        texts = {}
        if "tg" in rubric.platforms:
            texts["tg"] = main
            others = [p for p in rubric.platforms if p != "tg"]
            if others:
                adapted = self._ask(case_id, "adapt", settings.model_cheap, "adapt", {"tg": main})
                texts.update({p: adapted[p] for p in others})
        else:
            texts[rubric.platforms[0]] = main
        return {p: f"{t}\n\nПроверь своё резюме: {bot_link(rubric, p)}" for p, t in texts.items()}

    def _check(self, case_id: int, rubric: Rubric, texts: dict, source: dict) -> list[str]:
        # Сначала детерминированные правила — это основная защита
        problems = [f"{p}: {msg}" for p, t in texts.items() for msg in rules.check_text(t, rubric.link_tag, p)]
        if problems:
            return problems
        # Потом модель другого семейства ищет приукрашивание и насмешку
        review = self._ask(case_id, "review", settings.model_review, "review", {"source": source, "draft": texts})
        return [] if review.get("ok") else [f"проверка: {i}" for i in review.get("issues", ["не прошло"])]

    # --- решения людей ---
    def author_decision(self, draft_id: int, approved: bool) -> str:
        status = "waiting_editor" if approved else "author_rejected"
        self.db.update_draft(draft_id, status=status)
        self.db.log(self.db.get_draft(draft_id)["case_id"], "author", status)
        return status

    def apply_edit(self, draft_id: int, platform: str, new_text: str) -> list[str]:
        """Редактор правит текст. Правила проверяют правку ещё раз."""
        d = self.db.get_draft(draft_id)
        rubric = RUBRICS[self.db.get_case(d["case_id"])["rubric"]]
        texts = json.loads(d["texts"])
        texts[platform] = new_text
        problems = rules.check_text(new_text, rubric.link_tag, platform)
        self.db.update_draft(draft_id, texts=texts, problems=json.dumps(problems, ensure_ascii=False),
                             status="checks_failed" if problems else "waiting_editor")
        self.db.log(d["case_id"], "editor_edit", platform)
        return problems

    def approve(self, draft_id: int, editor_id: int) -> str:
        """Ставит выпуск в ближайшую неделю, где не превышены лимиты частоты из аудита."""
        d = self.db.get_draft(draft_id)
        if d["status"] != "waiting_editor":
            raise ValueError(f"черновик в статусе {d['status']}, утвердить нельзя")
        rubric = RUBRICS[self.db.get_case(d["case_id"])["rubric"]]
        day = datetime.now(timezone.utc)
        for _ in range(12):
            week = iso_week(day)
            rubric_ok = self.db.count_in_week(week, rubric.key) < rubric.weekly_limit
            posts = sum(self.db.count_in_week(week, r.key) for r in RUBRICS.values() if r.counts_as_post)
            total_ok = not rubric.counts_as_post or posts < settings.max_posts_per_week
            if rubric_ok and total_ok:
                self.db.update_draft(draft_id, status="approved", week=week, editor_id=editor_id)
                self.db.log(d["case_id"], "approved", week)
                return week
            day += timedelta(days=7)
        raise RuntimeError("нет свободной недели в ближайшие 12")

    # --- ролики: та же схема, другой формат ---
    def make_reels(self, draft_id: int, render: bool = True) -> list[int]:
        """Из проверенного черновика собираем 2–3 ролика. Источник — только обезличенные поля модели."""
        d = self.db.get_draft(draft_id)
        case = self.db.get_case(d["case_id"])
        rubric = RUBRICS[case["rubric"]]
        if d["status"] not in ("waiting_editor", "approved"):
            raise ValueError(f"черновик в статусе {d['status']}: ролики делаем только после подтверждения автора")
        draft = json.loads(d["draft_json"] or "{}")
        angles = reels.ANGLES.get(rubric.key, ())[: settings.reels_per_hero]
        source = json.loads(case["anonymized"] or "{}")
        ids = []
        for angle in angles:
            payload = reels.payload(rubric.key, draft, angle)
            sb, problems = None, []
            for attempt in range(2):
                raw = self._ask(case["id"], "storyboard", settings.model_draft, "storyboard", payload)
                sb = reels.finalize(raw, angle)
                problems = reels.check(sb)
                if not problems:
                    review = self._ask(case["id"], "review", settings.model_review, "review",
                                       {"source": source, "draft": {"reel": sb.voice_text}})
                    problems = [] if review.get("ok") else [f"проверка: {i}" for i in review.get("issues", [])]
                if not problems or attempt == 1:
                    break
                payload = {**payload, "fix_these_problems": problems}
                self.db.log(case["id"], "reel_redraft", "; ".join(problems))
            video = None
            if render and not problems:
                settings.output_dir.mkdir(parents=True, exist_ok=True)
                base = settings.output_dir / f"reel_{draft_id}_{angle}"
                audio = reels.synthesize(sb.voice_text, base.with_suffix(".mp3"))
                video = reels.render_video(sb, base.with_suffix(".mp4"), audio)
                if video is None:            # нет ffmpeg/Playwright — отдаём редактору HTML-превью
                    video = reels.render_html(sb, base.with_suffix(".html"))
            status = "checks_failed" if problems else ("waiting_author" if rubric.needs_consent else "waiting_editor")
            sb_dict = {"angle": sb.angle, "fact_key": sb.fact_key, "scenes": sb.scenes,
                       "seconds": reels.total_seconds(sb)}
            ids.append(self.db.add_reel(draft_id, angle, sb_dict, problems, status, str(video) if video else None))
            self.db.log(case["id"], "reel", f"{angle}: {status}")
        return ids

    def reel_author_decision(self, reel_id: int, approved: bool) -> str:
        status = "waiting_editor" if approved else "rejected"
        self.db.update_reel(reel_id, status=status)
        return status

    def approve_reel(self, reel_id: int) -> str:
        """Ролик встаёт в ближайшую неделю, где роликов меньше лимита (план: 7 в неделю)."""
        r = self.db.get_reel(reel_id)
        if r["status"] != "waiting_editor":
            raise ValueError(f"ролик в статусе {r['status']}, утвердить нельзя")
        day = datetime.now(timezone.utc)
        for _ in range(12):
            week = iso_week(day)
            if self.db.reels_in_week(week) < settings.reels_per_week:
                self.db.update_reel(reel_id, status="approved", week=week)
                return week
            day += timedelta(days=7)
        raise RuntimeError("нет свободной недели в ближайшие 12")

    def reject(self, draft_id: int, editor_id: int) -> None:
        self.db.update_draft(draft_id, status="rejected", editor_id=editor_id)
        self.db.log(self.db.get_draft(draft_id)["case_id"], "rejected")
