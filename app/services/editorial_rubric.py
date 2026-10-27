"""Код-рубрика редакции: один источник проверок для бенча и досье ревью.

Проверки бесплатны (без LLM): знаменатель процента, копи-паст точности,
тавтологии-роли, канцелярит, бюджет цифр, стена текста, повторы лида,
дубль заголовок-лид, слабые глаголы.
"""
from __future__ import annotations

import difflib
import re

CANC = ["осуществляет", "в рамках", "данная ситуация", "следует отметить",
        "в современном мире", "важно понимать", "нельзя не отметить", "представьте"]
TAUT = ["девелопер недвижимости", "девелопер жилья", "деловое медиа",
        "компания-застройщик жилья"]
DENOM = ["предложения", "объёма", "объема", "сделок", "рынка", "стройк",
         "кажд", "лот", "квартир", "стоимост", "ДДУ", "выдач", "ввод",
         "экспозиции", "жилья", "строящ", "половина"]
WEAK_VERBS = re.compile(r"(является|представляет собой|становится|происходит)")


def rubric_checks(title: str, body: str) -> list:
    bad = []
    paras = [p for p in body.split("\n\n") if p.strip()]
    first = paras[0] if paras else " "
    share = re.search(r"(доля|занимают|занимает|приходится|"
                      r"кажд\w+ (пятая|вторая|третья|четвертая|пятый|второй|третий|четвертый))",
                      first)
    if share and not any(w in first for w in DENOM):
        bad.append("denominator")
    if re.search(r"\d+,\d{2,}\s*%", body):
        bad.append("precision")
    for t in TAUT:
        if t in body:
            bad.append(f"taut:{t}")
    for c in CANC:
        if c in body:
            bad.append(f"canc:{c}")
    nums = re.findall(r"\d+[.,]?\d*\s*(?:%|₽|руб|млн|млрд|тыс|м²|кв)", body)
    if len(nums) > 10:
        bad.append(f"numbers:{len(nums)}")
    if len(body) > 900 and (len(paras) < 2 or
                            max((len(re.findall(r"[.!?](?:\s|$)",
                                                re.sub(r"(?:кв|тыс|млн|млрд|руб|г|м)\.\s*", "", p)))
                                 for p in paras), default=0) > 5):
        bad.append("wall")
    lead = first[:60]
    if lead and sum(1 for p in paras[1:] if lead[:40] in p):
        bad.append("repeat")
    if (title and first and difflib.SequenceMatcher(
            None, title.lower(), first[:len(title)].lower()).ratio() > 0.8):
        bad.append("title_lead_dup")
    wb = len(WEAK_VERBS.findall(body))
    if wb:
        bad.append(f"weak_verbs:{wb}")
    meta = len(re.findall(r"(?m)^(Проверяем гипотезу|Итог|Важная оговорка|Оговорка|Вывод)\s*:",
                          body))
    if meta:
        bad.append(f"meta_label:{meta}")
    return bad