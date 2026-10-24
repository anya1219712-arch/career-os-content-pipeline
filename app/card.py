"""Карточка выпуска по HTML-шаблону в цветах бренда (ДЗ 1): синий #1A3DFB, оранжевый #FA7614, Roboto.

Текст на картинке не генерирует нейросеть — он подставляется в шаблон, поэтому всегда точный.
PNG делается через Playwright, если он установлен; иначе остаётся HTML.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import settings

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))


def render_card(draft_id: int, draft: dict, fact) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    html = _env.get_template("card.html").render(
        hook=draft.get("hook", ""), wrote=draft.get("wrote", ""), fix=draft.get("fix", ""),
        fact=fact.text if fact else "", bot=settings.bot_username)
    path = settings.output_dir / f"card_{draft_id}.html"
    path.write_text(html, encoding="utf-8")
    return to_png(path) or path


def to_png(html_path: Path) -> Path | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    png = html_path.with_suffix(".png")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.goto(html_path.resolve().as_uri())
            page.screenshot(path=str(png))
            browser.close()
        return png
    except Exception:
        return None
