"""Демо без Telegram: прогоняет конвейер на примерах и печатает результат.

    python -m app.cli demo            # все три рубрики на демо-данных
    python -m app.cli demo --rubric slomanny_otklik
    python -m app.cli purge           # удалить исходники старше срока хранения
"""

import argparse
import json
from pathlib import Path

from .config import ROOT, settings
from .db import DB
from .pipeline import Pipeline

SAMPLES = json.loads((ROOT / "data" / "demo_cases.json").read_text(encoding="utf-8"))


def demo(rubric: str | None):
    db = DB(settings.db_path)
    pipe = Pipeline(db)
    print(f"Модели: {'заглушка (нет LLM_API_KEY)' if settings.use_mock_llm else settings.llm_base_url}\n")
    for sample in SAMPLES:
        if rubric and sample["rubric"] != rubric:
            continue
        case_id = db.add_case(sample["rubric"], sample["input"], sample.get("story", ""),
                              user_id=None, source_tag="demo", consent=sample.get("consent", True))
        res = pipe.run(case_id)
        print("=" * 70)
        print(f"{sample['title']} · кейс #{case_id} · статус: {res.status}")
        for p in res.problems:
            print(f"  ⚠ {p}")
        for platform, text in res.texts.items():
            print(f"\n--- {platform.upper()} ---\n{text}")
        if res.card_path:
            print(f"\nКарточка: {Path(res.card_path).resolve()}")
        if res.status == "waiting_author":
            pipe.author_decision(res.draft_id, True)
        if res.draft_id and db.get_draft(res.draft_id)["status"] == "waiting_editor":
            print(f"\nРедактор утвердил → неделя {pipe.approve(res.draft_id, editor_id=0)}")
        print()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--rubric", choices=["slomanny_otklik", "hr_eyes", "threads_reply"])
    sub.add_parser("purge")
    args = parser.parse_args()
    if args.cmd == "demo":
        demo(args.rubric)
    else:
        print("Удалено исходников:", DB(settings.db_path).purge_old(settings.retention_days))


if __name__ == "__main__":
    main()
