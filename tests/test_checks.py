from app.anonymize import anonymize, anonymize_rules, find_leftovers
from app.facts import find_unverified_numbers
from app.rules import check_text

LINK = "https://t.me/osresume_bot?start=molodets_tg"


def test_only_survey_numbers_allowed():
    assert find_unverified_numbers("10 из 10 рекрутеров ждут результат") == []
    assert find_unverified_numbers("6 из 10 отсеивают по формату") == []
    assert find_unverified_numbers("7 из 10 рекрутеров отсеют на этой фразе") == ["7 из 10"]
    assert find_unverified_numbers("семь рекрутеров из десяти") == ["семь рекрутеров из десяти"]
    assert find_unverified_numbers("охваты выросли на 40%") == ["40%"]


def test_rules_catch_tov_violations():
    assert "ToV: давление временем" in check_text(f"Рекрутер смотрит резюме 7 секунд. {LINK}", "molodets")
    assert "ToV: обещание оффера" in check_text(f"Гарантируем оффер за 7 дней {LINK}", "molodets")
    assert "ToV: одобрение компаний" in check_text(f"Мне это показала HR Яндекса {LINK}", "molodets")
    assert check_text(f"Спокойный честный разбор. {LINK}", "molodets") == []


def test_rules_require_link_and_length():
    assert any("нет ссылки" in p for p in check_text("Текст без ссылки", "molodets"))
    long_text = "а " * 300 + LINK
    assert any("длиннее" in p for p in check_text(long_text, "molodets", "threads"))


def test_anonymize_removes_contacts_and_companies():
    text = ("Иванова Анна Сергеевна, ООО «Ромашка», потом Яндекс. "
            "anna@mail.ru, +7 (900) 123-45-67, t.me/anna_cv, @anna_smm")
    clean, found = anonymize_rules(text)
    for leak in ["Иванова", "Ромашка", "Яндекс", "anna@mail.ru", "900", "t.me", "@anna_smm"]:
        assert leak not in clean
    assert {"телефон", "почта", "компания", "ФИО"} <= set(found)
    assert find_leftovers(clean) == []


def test_anonymize_keeps_normal_text():
    text = "Вела соцсети компании, участвовала в проектах, писала контент-план."
    assert anonymize_rules(text) == (text, [])
    assert anonymize(text)[0] == text


def test_ner_catches_what_rules_miss():
    # имя без фамилии, компания без ООО, город и проект правилами не ловятся
    text = "Работала с Петром в Ромашке в Казани, вела проект Альфа"
    assert anonymize_rules(text)[0] == text
    clean, found = anonymize(text)
    for leak in ["Петр", "Ромашк", "Казан", "Альфа"]:
        assert leak not in clean
    assert find_leftovers(clean) == []
