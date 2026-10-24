"""Ветка «ролики»: из одного проверенного черновика делаем 2–3 коротких анимированных ролика.

Почему так, а не генеративное видео:
- текст на экране подставляет программа в фирменный шаблон — нейросети-видеогенераторы
  путают кириллицу и могут «дорисовать» лишнее, а у нас каждая цифра проходит проверку;
- рендер идёт на нашем сервере (Playwright + ffmpeg): ничего не уходит за рубеж, оплаты нет;
- голос — Yandex SpeechKit (облако в РФ, оплата за символы), по желанию;
- генеративные фоны (например, Kandinsky) можно подключить отдельно, в базовый бюджет не входят.

Шаги: раскадровка (модель) → проверки (правила + вторая модель) → голос (SpeechKit, если есть ключ)
→ рендер MP4 → редактор утверждает → ролик встаёт в неделю с учётом лимита роликов.
"""

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import rules
from .config import settings
from .facts import fact_by_key, facts_for_prompt

ANGLES = {
    "molodets": ("expectation", "recruiter", "fix"),   # 2–3 ролика на одного героя
    "hr_eyes": ("tip",),                                # тот же механизм для постов рубрик
}
LABELS = {"hook": "", "did": "Что сделал", "wrote": "Что написал", "recruiter": "Что видит рекрутер",
          "fix": "Как переписать", "tip": "Правило отбора", "fact": "Опрос рекрутеров",
          "outro": "Career OS"}
MAX_CAPTION = 70
MAX_VOICE = 160
SIZE = (720, 1280)          # вертикальное видео 9:16

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))


@dataclass
class Storyboard:
    angle: str
    scenes: list[dict]
    fact_key: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def voice_text(self) -> str:
        return " ".join(s.get("voice", "") for s in self.scenes)


def source_fields(rubric: str, draft: dict) -> dict:
    """Что из черновика уходит в раскадровку: только уже обезличенные поля модели."""
    keys = ("hook", "did", "wrote", "recruiter_view", "fix") if rubric == "molodets" else ("title", "body")
    return {k: draft.get(k, "") for k in keys}


def finalize(raw: dict, angle: str) -> Storyboard:
    """Добавляем сцену с фактом из белого списка и считаем тайминги."""
    scenes = [dict(s) for s in raw.get("scenes", []) if s.get("caption")]
    fact = fact_by_key(raw.get("fact_key", ""))
    if fact:
        pos = max(len(scenes) - 1, 0)                         # перед финальной сценой
        scenes.insert(pos, {"kind": "fact", "caption": fact.text, "voice": fact.text + "."})
    start = 0.0
    for s in scenes:
        s["label"] = LABELS.get(s.get("kind", ""), "")
        # длительность по объёму озвучки: ~14 знаков в секунду, не меньше 2,5 с
        s["seconds"] = round(max(2.5, len(s.get("voice", "")) / 14 + 0.6), 1)
        s["start"] = round(start, 1)
        start += s["seconds"]
    return Storyboard(angle, scenes, raw.get("fact_key", ""))


def check(sb: Storyboard) -> list[str]:
    """Детерминированные проверки: те же правила ToV и белый список цифр, плюс длина."""
    problems = []
    for i, s in enumerate(sb.scenes, 1):
        for part in ("caption", "voice"):
            problems += [f"сцена {i}: {p}" for p in rules.check_text(s.get(part, ""))]
        if len(s.get("caption", "")) > MAX_CAPTION and s.get("kind") != "fact":
            problems.append(f"сцена {i}: текст на экране длиннее {MAX_CAPTION} знаков")
        if len(s.get("voice", "")) > MAX_VOICE:
            problems.append(f"сцена {i}: озвучка длиннее {MAX_VOICE} знаков")
    if not sb.scenes or sb.scenes[-1].get("kind") != "outro":
        problems.append("нет финальной сцены «Молодец, надо переписать»")
    return problems


def total_seconds(sb: Storyboard) -> float:
    return round(sum(s["seconds"] for s in sb.scenes), 1)


def render_html(sb: Storyboard, path: Path) -> Path:
    html = _env.get_template("reel.html").render(
        scenes=sb.scenes, total=total_seconds(sb), w=SIZE[0], h=SIZE[1],
        rubric="Молодец, надо переписать", bot=settings.bot_username)
    path.write_text(html, encoding="utf-8")
    return path


# ---------- голос: Yandex SpeechKit (РФ), только если задан ключ ----------

def synthesize(text: str, out: Path) -> Path | None:
    if not settings.speechkit_api_key:
        return None
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"text": text, "lang": "ru-RU", "voice": settings.speechkit_voice,
                                   "format": "mp3", "folderId": settings.speechkit_folder_id}).encode()
    req = urllib.request.Request("https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize", data=data,
                                 headers={"Authorization": f"Api-Key {settings.speechkit_api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out.write_bytes(r.read())
        return out
    except Exception:
        return None          # нет голоса — выпускаем ролик с субтитрами, редактор увидит пометку


# ---------- рендер: Playwright записывает анимацию, ffmpeg собирает MP4 ----------

def render_video(sb: Storyboard, out_mp4: Path, audio: Path | None = None) -> Path | None:
    """Возвращает MP4 или None, если на машине нет Playwright/ffmpeg (тогда остаётся HTML)."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        html = render_html(sb, Path(tmp) / "reel.html")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": SIZE[0], "height": SIZE[1]},
                                      record_video_dir=tmp, record_video_size={"width": SIZE[0], "height": SIZE[1]})
            page = ctx.new_page()
            page.goto(html.resolve().as_uri())
            time.sleep(total_seconds(sb) + 0.5)
            webm = Path(page.video.path())
            ctx.close()
            browser.close()
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm)]
        if audio:
            cmd += ["-i", str(audio), "-shortest", "-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(out_mp4)]
        subprocess.run(cmd, check=True)
    return out_mp4


def save_storyboard(sb: Storyboard, path: Path) -> Path:
    path.write_text(json.dumps({"angle": sb.angle, "fact_key": sb.fact_key, "scenes": sb.scenes,
                                "seconds": total_seconds(sb)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def payload(rubric: str, draft: dict, angle: str) -> dict:
    return {"angle": angle, "draft": source_fields(rubric, draft), "facts": facts_for_prompt()}
