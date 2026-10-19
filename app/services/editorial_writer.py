"""Фаза 3 виртуальной редакции: писатель — материалы темы → вердикт → статья → Post на ревью.

Для тем status=IN_WORK без статьи: собираем материалы (заголовки темы, тексты tg-постов,
web-заголовки) + справку из базы знаний (facts_kb), один вызов писателя: вердикт
write|drop + черновик в JSON. write -> строка articles (status=REVIEW) + post штатного
конвейера (status=AWAITING_REVIEW, ручной источник virtual_editorial, черновик-версия
origin=editorial, канал назначения = target_channels.editorial), тема -> READY,
заголовки -> used. drop -> тема -> DROPPED с причиной в verdict_note.
Публикация — только через штатное ревью (карточка бота); auto_publish в v1 фазы не_used.

Маршрутизация писателя по виду темы: rewrite -> editorial.writer_model,
hypothesis -> editorial.writer_model_hypothesis (пусто -> та же модель).
Редактор всегда на цепи writer_model: скоуп правок зависит от tier писателя.
"""
from __future__ import annotations

import difflib
import logging
import re
import re

from sqlalchemy import select

from app.db.enums import (ArticleStatus, DraftOrigin, EventActor, LLMStage, PostStatus,
                          SourceKind, TopicStatus)
from app.db.models import (Article, Headline, Material, Post, PostDraftVersion,
                           PostEvent, Source, TargetChannel, Topic)
from app.db.session import session_scope
from pathlib import Path

from app.services import facts_kb
from app.services.editorial_journalist import _call_json
from app.services.llm.prompts import EDITOR_SYSTEM, EDITOR_USER, WRITER_SYSTEM, WRITER_USER
from app.services.llm.schemas import EditorialArticleResult, EditorResult
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_now
from app.redis_client import get_redis

log = logging.getLogger("editorial_writer")

ARTICLES_PER_CYCLE = 1            # дисциплина редакции: 1 статья за цикл, 1-3 поста в день
_SYNTH_MSG_BASE = 9_000_000_000   # синтетические source_message_id редакционных постов
_SOURCE_USERNAME = "virtual_editorial"
_BRAND_DIR = Path(__file__).resolve().parent / "editorial_brand"

def _brand_block() -> str:
    """Редакционная память стиля: голос, запреты, примеры хорошо/плохо."""
    parts = []
    for name in ("voice.md", "forbidden.md", "examples_good.md", "examples_bad.md"):
        p = _BRAND_DIR / name
        if p.exists():
            parts.append(f"### {name}\n{p.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts) or "—"

_EDITOR_SCOPE = {
    "flash": ("Rule-repair is MANDATORY: fix every violation of the numbered rules and brand memory "
              "(precision, tautology roles, filler sentences, denominator, repetition, wall) with "
              "through-text passes; paragraph or lead rebuild allowed ONLY when a structural rule is "
              "violated; taste-level restyling of working sentences is FORBIDDEN."),
    "strong": ("Strict surgery: touch ONLY explicit violations of the numbered rules and brand memory; "
               "if the draft reads clean against the rules — verdict pass with zero edits; taste-level "
               "restyling and paragraph rebuilds are FORBIDDEN even when you would phrase it differently."),
}

async def _writer_models() -> tuple[str, str, dict | None]:
    """editorial.writer_model (rewrite) и editorial.writer_model_hypothesis
    (hypothesis; пусто -> та же, что writer); провайдеры общие."""
    async with session_scope() as session:
        model = (str(await get_setting(session, Keys.EDITORIAL_WRITER_MODEL) or "").strip()
                 or str(await get_setting(session, Keys.REVISION_MODEL) or "").strip())
        hypo = (str(await get_setting(
            session, getattr(Keys, "EDITORIAL_WRITER_MODEL_HYPOTHESIS",
                             "editorial.writer_model_hypothesis")) or "").strip()
                or model)
        providers = await get_providers(session, Keys.REVISION_PROVIDERS)
    return model, hypo, providers

async def _review_dossier(session, topic, heads, listing, kb,
                          final_text, ed_report, w_model, tier) -> dict:
    """Досье карточки ревью: всё считается кодом в момент написания статьи."""
    from app.services.editorial_rubric import rubric_checks
    title_line, _, body = final_text.partition("\n\n")
    fails = rubric_checks(title_line, body)
    recent = (await session.execute(
        select(Topic.theme).order_by(Topic.id.desc()).limit(15))).scalars().all()
    pct, nearest = 0, ""
    for r in recent:
        ratio = difflib.SequenceMatcher(None, topic.theme.lower(), r.lower()).ratio()
        if ratio > pct:
            pct, nearest = ratio, r
    anchors = re.findall(r"\d+[.,]?\d*|«[^»]+»", topic.hypothesis or "")
    found = [a for a in anchors if a in listing]
    sources = sorted({h.source_name or "—" for h in heads})
    fixed = []
    if ed_report:
        fixed = [k for k, v in (ed_report.get("check") or {}).items()
                 if "исправлено" in str(v).lower() or "fixed" in str(v).lower()]
    return {
        "rubric": fails,
        "novelty": {"pct": round(pct * 100), "nearest": nearest if pct > 0.3 else ""},
        "support": {"found": len(found), "total": len(anchors)},
        "materials": {"heads": len(heads), "sources": len(sources)},
        "editor": {"edits": len((ed_report or {}).get("edits") or []), "fixed": fixed},
        "writer": {"model": w_model.split("/")[-1], "tier": tier},
        "kb": (kb or "")[:300],
    }


async def _ensure_source(session) -> int:
    row = (await session.execute(
        select(Source.id).where(Source.username == _SOURCE_USERNAME))).scalar_one_or_none()
    if row is not None:
        return row
    src = Source(kind=SourceKind.EXTERNAL, title="Виртуальная редакция",
                 username=_SOURCE_USERNAME, manual=True, enabled=True)
    session.add(src)
    await session.flush()
    return src.id

async def _editorial_target(session):
    return (await session.execute(
        select(TargetChannel.id)
        .where(TargetChannel.editorial.is_(True), TargetChannel.enabled.is_(True))
        .order_by(TargetChannel.id).limit(1))).scalar_one_or_none()

async def _gather(session, topic):
    """Материалы темы: заголовки + тексты tg-постов + справка базы знаний."""
    ids = [int(x) for x in (topic.materials or []) if str(x).strip().isdigit()]
    heads = []
    if ids:
        heads = (await session.execute(
            select(Headline).where(Headline.id.in_(ids)))).scalars().all()
    texts = {}
    post_ids = [h.post_id for h in heads if h.post_id]
    if post_ids:
        rows = (await session.execute(
            select(Post.id, Post.original_text).where(Post.id.in_(post_ids)))).all()
        texts = {pid: (t or "")[:1500] for pid, t in rows}
    blocks = []
    for n, h in enumerate(heads, 1):
        body = texts.get(h.post_id)
        when = h.fetched_at.strftime("%d.%m.%Y") if h.fetched_at else ""
        if h.source_kind == "tg" and body:
            blocks.append(f"{n}. [tg {h.source_name or ''} {when}]\n{h.title}\n{body}")
        else:
            blocks.append(f"{n}. [web {h.source_name or ''} {when}]\n{h.title}"
                          + (f"\nurl: {h.url}" if h.url else ""))
        session.add(Material(topic_id=topic.id, url=h.url, post_id=h.post_id,
                             title=h.title, full_text=body or None))
    listing = "\n\n".join(blocks) or "—"
    kb = facts_kb.format_context(facts_kb.search(f"{topic.theme} {topic.hypothesis or ''}"))
    return heads, listing, kb

async def run_writer_phase() -> int:
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.EDITORIAL_ENABLED))
        budget = float(await get_setting(session, Keys.EDITORIAL_BUDGET_USD_PER_DAY))
        max_chars = int(await get_setting(session, Keys.EDITORIAL_POST_MAX_CHARS))
    if not enabled:
        return 0
    day = owner_now().date().isoformat()
    spent = float(await get_redis().get(f"guard:editorial_cost:{day}") or 0)
    if spent >= budget:
        log.warning("writer: бюджет редакции исчерпан (%.2f/%.2f) — статьи не пишем",
                    spent, budget)
        return 0
    async with session_scope() as session:
        topics = (await session.execute(
            select(Topic)
            .where(Topic.status == TopicStatus.IN_WORK,
                   ~Topic.id.in_(select(Article.topic_id)))
            .order_by(Topic.id).limit(ARTICLES_PER_CYCLE * 2))).scalars().all()
    model, hypo_model, providers = await _writer_models()
    written = 0
    for topic in topics[:ARTICLES_PER_CYCLE]:
        async with session_scope() as session:
            t = await session.get(Topic, topic.id)
            heads, listing, kb = await _gather(session, t)
            bare = re.sub(r"\d{1,2}\.\d{1,2}\.\d{2,4}", "", listing)  # даты меток — не богатство материалов
            rich = sum(ch.isdigit() for ch in bare) >= 8
            w_model = hypo_model if (t.kind.value == "hypothesis" and rich) else model
            tier = "strong" if w_model != model else "flash"
            messages = [
                {"role": "system",
                 "content": WRITER_SYSTEM.format(max_chars=max_chars)
                 + "\n\nBRAND AND STYLE MEMORY:\n" + _brand_block()},
                {"role": "user", "content": WRITER_USER.format(
                    theme=t.theme, hypothesis=t.hypothesis or "—",
                    kind=t.kind.value, materials=listing, kb=kb)},
            ]
            try:
                res = await _call_json(messages, w_model, providers, 5000,
                                       EditorialArticleResult,
                                       stage=LLMStage.EDITORIAL_WRITE)
            except Exception as exc:  # noqa: BLE001
                log.warning("writer: тема #%s без статьи (технически): %s", t.id, exc)
                continue
            if res.verdict == "drop":
                t.status = TopicStatus.DROPPED
                t.verdict = "refuted"
                t.verdict_note = res.drop_reason or "писатель: материалов недостаточно"
                await session.commit()
                log.info("writer: тема #%s отклонена: %s", t.id, t.verdict_note)
                continue
            ed_report = None
            try:
                ed = await _call_json(
                    [{"role": "system",
                      "content": EDITOR_SYSTEM.format(max_chars=max_chars)
                      + "\n\nBRAND AND STYLE MEMORY:\n" + _brand_block()
                      + "\n\nEDITOR SCOPE (writer tier: " + tier + "):\n"
                      + _EDITOR_SCOPE[tier]},
                     {"role": "user",
                      "content": EDITOR_USER.format(materials=listing, kb=kb,
                                                    draft=res.text)}],
                    model, providers, 4000, EditorResult,
                    stage=LLMStage.EDITORIAL_WRITE)
                if ed.verdict == "rewrite" and ed.text:
                    log.info("writer: редактор переписал статью темы #%s: %s",
                             t.id, "; ".join(ed.problems or [])[:160])
                    res.text = ed.text
                ed_report = {"edits": ed.edits or [], "check": ed.check or {}}
            except Exception as exc:  # noqa: BLE001
                log.warning("writer: редактор не смог проверить тему #%s — черновик как есть: %s",
                            t.id, exc)
            final_text = (f"{res.title.strip()}\n\n{res.text.strip()}"
                          if res.title.strip() else res.text.strip())
            review = await _review_dossier(session, t, heads, listing, kb,
                                           final_text, ed_report, w_model, tier)
            art = Article(topic_id=t.id, draft_text=final_text,
                          status=ArticleStatus.REVIEW, review=review)
            session.add(art)
            await session.flush()
            src_id = await _ensure_source(session)
            tgt_id = await _editorial_target(session)
            post = Post(source_id=src_id,
                        source_message_id=_SYNTH_MSG_BASE + art.id,
                        original_text=final_text, draft_text=final_text,
                        status=PostStatus.AWAITING_REVIEW,
                        target_channel_id=tgt_id, autopilot=False)
            session.add(post)
            await session.flush()
            session.add(PostDraftVersion(post_id=post.id, version=1,
                                         text=final_text, origin=DraftOrigin.EDITORIAL))
            if ed_report and (ed_report["edits"] or ed_report["check"]):
                session.add(PostEvent(post_id=post.id, actor=EventActor.SYSTEM,
                                      action="editor_report", details=ed_report))
            for h in heads:
                hh = await session.get(Headline, h.id)
                if hh is not None:
                    hh.status = "used"
            t.status = TopicStatus.READY
            t.verdict = "confirmed"
            await session.commit()
            written += 1
            log.info("writer: статья #%s (тема #%s, %s, writer=%s): %s | post #%s -> AWAITING_REVIEW",
                     art.id, t.id, t.kind.value, w_model.split("/")[-1],
                     (res.title or "")[:60], post.id)
    return written