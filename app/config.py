"""Настройки берём из переменных окружения или файла .env (см. .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Простой загрузчик .env, чтобы не тянуть лишнюю библиотеку."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip()       # комментарий после значения
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


@dataclass
class Settings:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")
    editor_chat_id: int = int(os.getenv("EDITOR_CHAT_ID", "0") or 0)
    editor_ids: list[int] = field(default_factory=lambda: _int_list(os.getenv("EDITOR_IDS", "")))

    # Модели: OpenAI-совместимый API (Selectel Token Factory). Адрес и ключ — из панели Selectel.
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    # Резервный провайдер (Cloud.ru / Yandex AI Studio), тоже OpenAI-совместимый
    llm_fallback_base_url: str = os.getenv("LLM_FALLBACK_BASE_URL", "")
    llm_fallback_api_key: str = os.getenv("LLM_FALLBACK_API_KEY", "")

    # Модель под каждый шаг — гипотеза, выбирается тестом (см. README)
    model_cheap: str = os.getenv("MODEL_CHEAP", "Qwen-3.8-Flash-Next")      # обезличивание, отбор, адаптация
    model_draft: str = os.getenv("MODEL_DRAFT", "Qwen-3.8-Flash-Next")      # черновик выпуска
    model_review: str = os.getenv("MODEL_REVIEW", "DeepSeek-V4-Flash")      # проверка, другое семейство

    # Без ключа работаем на заглушке — удобно для демо и тестов
    use_mock_llm: bool = os.getenv("USE_MOCK_LLM", "").lower() in ("1", "true", "yes") or not os.getenv("LLM_API_KEY")

    # Данные и правила
    db_path: Path = Path(os.getenv("DB_PATH", str(ROOT / "data" / "pipeline.db")))
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", str(ROOT / "output")))
    retention_days: int = int(os.getenv("RETENTION_DAYS", "30"))
    max_posts_per_week: int = int(os.getenv("MAX_POSTS_PER_WEEK", "5"))   # аудит ДЗ 2
    special_per_week: int = int(os.getenv("SPECIAL_PER_WEEK", "1"))        # слот «Почему не зовут»
    bot_username: str = os.getenv("BOT_USERNAME", "osresume_bot")
    timezone: str = os.getenv("TZ_NAME", "Europe/Moscow")

    # Ролики: рендер на своём сервере, голос — Yandex SpeechKit (необязательно)
    reels_per_hero: int = int(os.getenv("REELS_PER_HERO", "3"))     # из истории одного героя 2–3 ролика
    reels_per_week: int = int(os.getenv("REELS_PER_WEEK", "7"))     # план слайда 8: 7 роликов в неделю
    speechkit_api_key: str = os.getenv("SPEECHKIT_API_KEY", "")
    speechkit_folder_id: str = os.getenv("SPEECHKIT_FOLDER_ID", "")
    speechkit_voice: str = os.getenv("SPEECHKIT_VOICE", "alena")


settings = Settings()
