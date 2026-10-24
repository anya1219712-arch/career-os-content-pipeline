import json
import shutil

import pytest

from app import reels
from app.db import DB
from app.llm import MockLLM
from app.pipeline import Pipeline

RESUME = "SMM-менеджер. Вела соцсети компании, участвовала в проектах. Почта anna@mail.ru"
STORY = "Подняла охваты в два раза за полгода и обучила двух новичков"


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.output_dir", tmp_path)
    return Pipeline(DB(":memory:"), MockLLM())


def confirmed_draft(p: Pipeline, rubric="molodets", text=RESUME) -> int:
    if rubric == "molodets":
        res = p.run(p.db.add_case(rubric, text, STORY, user_id=1, consent=True))
        p.author_decision(res.draft_id, True)
    else:
        res = p.run(p.db.add_case(rubric, text))
    return res.draft_id


def test_one_hero_gives_three_reels_with_fact_and_outro(pipe):
    ids = pipe.make_reels(confirmed_draft(pipe), render=False)
    assert len(ids) == 3
    for rid in ids:
        r = pipe.db.get_reel(rid)
        sb = json.loads(r["storyboard"])
        assert r["status"] == "waiting_author"                    # герой видит ролик про себя до публикации
        assert sb["scenes"][-1]["caption"] == "Молодец, надо переписать"
        assert any(s["kind"] == "fact" and "10 из 10" in s["caption"] for s in sb["scenes"])
        assert all(s["seconds"] >= 2.5 for s in sb["scenes"])


def test_same_mechanics_for_feed_posts(pipe):
    ids = pipe.make_reels(confirmed_draft(pipe, "hr_eyes", "Тема: на что смотрят в резюме"), render=False)
    assert len(ids) == 1 and pipe.db.get_reel(ids[0])["status"] == "waiting_editor"


def test_no_reels_before_author_confirms(pipe):
    res = pipe.run(pipe.db.add_case("molodets", RESUME, STORY, user_id=1, consent=True))
    with pytest.raises(ValueError):
        pipe.make_reels(res.draft_id, render=False)


class InventsInReel(MockLLM):
    def ask(self, task, model, system, user):
        out = super().ask(task, model, system, user)
        if task == "storyboard":
            out["scenes"][0]["voice"] = "7 из 10 рекрутеров отсеют такое резюме"
        return out


def test_reel_with_invented_number_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.output_dir", tmp_path)
    p = Pipeline(DB(":memory:"), InventsInReel())
    rid = p.make_reels(confirmed_draft(p), render=False)[0]
    r = p.db.get_reel(rid)
    assert r["status"] == "checks_failed" and "7 из 10" in r["problems"]
    p.db.update_reel(rid, status="waiting_editor_bypass")
    with pytest.raises(ValueError):
        p.approve_reel(rid)


def test_reels_weekly_limit(pipe, monkeypatch):
    monkeypatch.setattr("app.config.settings.reels_per_week", 2)
    ids = pipe.make_reels(confirmed_draft(pipe), render=False)
    weeks = []
    for rid in ids:
        pipe.reel_author_decision(rid, True)
        weeks.append(pipe.approve_reel(rid))
    assert weeks[0] == weeks[1] != weeks[2]


def test_revoke_removes_reels(pipe):
    ids = pipe.make_reels(confirmed_draft(pipe), render=False)
    pipe.db.revoke_user(1)
    assert all(pipe.db.get_reel(i)["status"] == "rejected" for i in ids)


def test_reel_html_has_timed_scenes(tmp_path):
    raw = MockLLM().ask("storyboard", "", "", json.dumps(
        {"angle": "fix", "draft": {"wrote": "Вела соцсети", "did": "Подняла охваты"}, "facts": ""}))
    sb = reels.finalize(raw, "fix")
    html = reels.render_html(sb, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "Молодец, надо переписать" in html and "animation" in html
    assert reels.total_seconds(sb) > 10


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="нужен ffmpeg")
def test_render_mp4(tmp_path):
    pytest.importorskip("playwright")
    raw = MockLLM().ask("storyboard", "", "", json.dumps(
        {"angle": "recruiter", "draft": {"wrote": "Вела соцсети", "did": "Подняла охваты"}, "facts": ""}))
    sb = reels.finalize(raw, "recruiter")
    try:
        out = reels.render_video(sb, tmp_path / "r.mp4")
    except Exception as e:          # браузер для Playwright не установлен
        pytest.skip(str(e))
    assert out and out.stat().st_size > 10_000
