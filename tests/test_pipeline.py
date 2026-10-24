import json

import pytest

from app.db import DB
from app.llm import MockLLM
from app.pipeline import Pipeline

RESUME = "SMM-менеджер. Вела соцсети компании, участвовала в проектах. Почта anna@mail.ru"
STORY = "Подняла охваты в два раза за полгода и обучила двух новичков"


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.output_dir", tmp_path)
    return Pipeline(DB(":memory:"), MockLLM())


def test_no_consent_stops(pipe):
    cid = pipe.db.add_case("slomanny_otklik", RESUME, STORY, user_id=1, consent=False)
    assert pipe.run(cid).status == "stopped_no_consent"
    assert pipe.db.drafts_by_status("waiting_author") == []


def test_full_flow_goes_through_author_and_editor(pipe):
    cid = pipe.db.add_case("slomanny_otklik", RESUME, STORY, user_id=1, consent=True)
    res = pipe.run(cid)
    assert res.status == "waiting_author"
    assert set(res.texts) == {"tg", "threads", "vk"}
    for platform, text in res.texts.items():
        assert f"start=slomanny_otklik_{platform}" in text
        assert "anna@mail.ru" not in text
    assert "10 из 10" in res.texts["tg"]          # цифра подставлена из белого списка
    with pytest.raises(ValueError):                # без автора редактор утвердить не может
        pipe.approve(res.draft_id, editor_id=7)
    pipe.author_decision(res.draft_id, approved=True)
    week = pipe.approve(res.draft_id, editor_id=7)
    assert pipe.db.get_draft(res.draft_id)["week"] == week


def test_weekly_limit_moves_second_issue_to_next_week(pipe):
    weeks = []
    for _ in range(2):
        cid = pipe.db.add_case("slomanny_otklik", RESUME, STORY, user_id=1, consent=True)
        res = pipe.run(cid)
        pipe.author_decision(res.draft_id, True)
        weeks.append(pipe.approve(res.draft_id, editor_id=7))
    assert weeks[0] != weeks[1]


def test_same_mechanics_for_other_rubrics(pipe):
    for rubric, text in [("hr_eyes", "Тема: на что смотрят в резюме"),
                         ("threads_reply", "Посмотрите резюме: 4 года в маркетинге, ноль приглашений")]:
        res = pipe.run(pipe.db.add_case(rubric, text))
        assert res.status == "waiting_editor"
        assert all(f"start={rubric}" in t for t in res.texts.values())


class InventsNumbers(MockLLM):
    """Модель, которая упорно вписывает цифру не из опроса."""
    def ask(self, task, model, system, user):
        out = super().ask(task, model, system, user)
        if task == "draft_slomanny_otklik":
            out["hook"] = "7 из 10 рекрутеров отсеют такое резюме"
        return out


class ReviewerFindsEmbellishment(MockLLM):
    def ask(self, task, model, system, user):
        if task == "review":
            return {"ok": False, "issues": ["в черновике рост охватов в 3 раза, в исходнике — в 2"]}
        return super().ask(task, model, system, user)


@pytest.mark.parametrize("llm,expected", [(InventsNumbers(), "цифра не из опроса"),
                                          (ReviewerFindsEmbellishment(), "проверка:")])
def test_failed_checks_block_issue_after_one_redraft(tmp_path, monkeypatch, llm, expected):
    monkeypatch.setattr("app.config.settings.output_dir", tmp_path)
    p = Pipeline(DB(":memory:"), llm)
    res = p.run(p.db.add_case("slomanny_otklik", RESUME, STORY, user_id=1, consent=True))
    assert res.status == "checks_failed"
    assert any(expected in x for x in res.problems)
    redrafts = p.db.conn.execute("SELECT COUNT(*) FROM log WHERE step='redraft'").fetchone()[0]
    assert redrafts == 1
    with pytest.raises(ValueError):
        p.approve(res.draft_id, editor_id=7)


def test_editor_edit_is_rechecked(pipe):
    cid = pipe.db.add_case("hr_eyes", "Тема: фильтры до человека")
    res = pipe.run(cid)
    assert pipe.apply_edit(res.draft_id, "tg", "Рекрутер смотрит 6 секунд") != []
    assert pipe.db.get_draft(res.draft_id)["status"] == "checks_failed"


def test_revoke_deletes_data(pipe):
    cid = pipe.db.add_case("slomanny_otklik", RESUME, STORY, user_id=42, consent=True)
    res = pipe.run(cid)
    assert pipe.db.revoke_user(42) == [cid]
    case = pipe.db.get_case(cid)
    assert case["raw_input"] is None and case["real_story"] is None and case["status"] == "revoked"
    assert json.loads(pipe.db.get_draft(res.draft_id)["texts"]) == {}


def test_purge_removes_old_sources(pipe):
    cid = pipe.db.add_case("slomanny_otklik", RESUME, STORY, user_id=1, consent=True)
    pipe.db.conn.execute("UPDATE cases SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (cid,))
    assert pipe.db.purge_old(30) == 1
    assert pipe.db.get_case(cid)["raw_input"] is None
