"""B1: гейт Editor-in-Chief перед автопубликацией статей редакции.

Полный контекст (материалы + БЗ + тема + черновик) на сильной модели: фактическая
дисциплина, оговорки масштаба, отработка гипотезы. Режимы (editorial.gate_mode):
  off     — гейт не вызывается;
  shadow  — вердикт пишется в досье статьи и post_events, публикация остаётся ручной;
  enforce — гейт решает судьбу поста: approve -> APPROVED и задача публикации в окнах;
            revise -> один проход редактора по заметкам и повторный гейт (один круг);
            reject -> UNSUITABLE + дроп темы с причиной.
Предохранители: sig-guard на последней строке, бюджет/окна/лимиты наследуются
штатной машиной публикаций; сбой гейта всегда оставляет пост ручным.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.enums import (DraftOrigin, EventActor, LLMStage, PostStatus,
                          PublishMode, TopicStatus)
from app.db.models import Article, Post, PostDraftVersion, PostEvent, Topic
from app.db.session import session_scope
from app.services.editorial_journalist import _call_json
from app.services.llm.prompts import (EDITOR_SYSTEM, EDITOR_USER,
                                      GATE_SYSTEM, GATE_USER)
from app.services.llm.schemas import EditorResult, GateResult
from app.services.llm_pipeline import _sig_guard_hit
from app.services.publishing import create_publish_job
from app.services.settings import Keys, get_providers, get_setting

log = logging.getLogger("editorial_gate")


async def _gate_model() -> tuple[str, dict | None]:
    async with session_scope() as session:
        model = str(await get_setting(session, Keys.EDITORIAL_GATE_MODEL) or "").strip()
        if not model:
            model = str(await get_setting(
                session, getattr(Keys, "EDITORIAL_WRITER_MODEL_HYPOTHESIS",
                                 "editorial.writer_model_hypothesis")) or "").strip()
        if not model:
            model = str(await get_setting(session, Keys.EDITORIAL_WRITER_MODEL) or "").strip()
        providers = await get_providers(session, Keys.REVISION_PROVIDERS)
    return model, providers


async def _run_gate(topic, listing: str, kb: str, draft: str) -> GateResult:
    model, providers = await _gate_model()
    return await _call_json(
        [{"role": "system", "content": GATE_SYSTEM},
         {"role": "user", "content": GATE_USER.format(
             theme=topic.theme, hypothesis=topic.hypothesis or "—",
             kind=topic.kind.value, materials=listing, kb=kb, draft=draft)}],
        model, providers, 1500, GateResult, stage=LLMStage.EDITORIAL_GATE)


async def _approve(post_id: int, details: dict) -> None:
    async with session_scope() as session:
        p = await session.get(Post, post_id)
        if p is None or p.status is not PostStatus.AWAITING_REVIEW:
            return
        p.status = PostStatus.APPROVED
        p.approved_at = datetime.now(timezone.utc)
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                              action="gate_approved",
                              from_status=PostStatus.AWAITING_REVIEW.value,
                              to_status=PostStatus.APPROVED.value, details=details))
        await session.commit()
    mode = getattr(PublishMode, "SCHEDULED", PublishMode.NOW)
    ok, msg = await create_publish_job(post_id, mode)
    log.info("gate: пост #%s -> APPROVED, задача публикации: %s", post_id, msg)


async def _reject(post_id: int, topic_id: int, reason: str, details: dict) -> None:
    async with session_scope() as session:
        p = await session.get(Post, post_id)
        if p is not None:
            p.status = PostStatus.UNSUITABLE
            p.reject_reason = f"гейт: {reason or 'reject'}"
        t = await session.get(Topic, topic_id)
        if t is not None:
            t.status = TopicStatus.DROPPED
            t.verdict = "refuted"
            t.verdict_note = f"гейт: {reason or 'reject'}"
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                              action="gate_rejected", details=details))
        await session.commit()
    log.info("gate: пост #%s отклонён: %s", post_id, reason)


async def _revise_once(post_id: int, listing: str, kb: str,
                       draft: str, notes: list) -> str | None:
    """Один проход редактора по заметкам гейта; возвращает новый полный текст."""
    async with session_scope() as session:
        max_chars = int(await get_setting(session, Keys.EDITORIAL_POST_MAX_CHARS))
        model = str(await get_setting(session, Keys.EDITORIAL_WRITER_MODEL) or "").strip()
        providers = await get_providers(session, Keys.REVISION_PROVIDERS)
    try:
        ed = await _call_json(
            [{"role": "system",
              "content": EDITOR_SYSTEM.format(max_chars=max_chars)
              + "\n\nGATE NOTES (обязательны к отработке, каждая):\n- "
              + "\n- ".join(notes)},
             {"role": "user", "content": EDITOR_USER.format(
                 materials=listing, kb=kb, draft=draft)}],
            model, providers, 4000, EditorResult, stage=LLMStage.EDITORIAL_WRITE)
    except Exception as exc:  # noqa: BLE001
        log.warning("gate: пост #%s — проход редактора не удался: %s", post_id, exc)
        return None
    if not ed.text:
        return None
    title_line = draft.split("\n\n", 1)[0]
    new_final = f"{title_line}\n\n{ed.text.strip()}"
    async with session_scope() as session:
        p = await session.get(Post, post_id)
        if p is None:
            return None
        p.draft_text = new_final
        p.original_text = new_final
        p.draft_version += 1
        session.add(PostDraftVersion(post_id=post_id, version=p.draft_version,
                                     text=new_final, origin=DraftOrigin.EDITORIAL))
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                              action="gate_revise", details={"notes": notes}))
        await session.commit()
    return new_final


async def gate_step(post_id: int, art_id: int, topic_id: int,
                    listing: str, kb: str, draft: str) -> None:
    async with session_scope() as session:
        mode = str(await get_setting(session, Keys.EDITORIAL_GATE_MODE) or "off").strip().lower()
        t = await session.get(Topic, topic_id)
    if mode == "off" or t is None:
        return
    try:
        g = await _run_gate(t, listing, kb, draft)
    except Exception as exc:  # noqa: BLE001 — гейт недоступен: пост остаётся ручным
        log.warning("gate: пост #%s — гейт недоступен (%s), оставлен ручным",
                    post_id, exc.__class__.__name__)
        return
    details = {"verdict": g.verdict, "notes": g.notes or [],
               "reason": g.reason or "", "mode": mode}
    log.info("gate: пост #%s — %s%s", post_id, g.verdict.upper(),
             f" ({g.reason})" if g.reason else "")
    if mode == "shadow":
        async with session_scope() as session:
            art = await session.get(Article, art_id)
            if art is not None:
                review = dict(art.review or {})
                review["gate"] = details
                art.review = review
            session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                                  action="gate_verdict", details=details))
            await session.commit()
        return
    # enforce
    if g.verdict == "approve":
        if _sig_guard_hit(draft):
            async with session_scope() as session:
                p = await session.get(Post, post_id)
                if p is not None:
                    p.double_check_note = ("гейт одобрил, но сработал предохранитель "
                                           "подписей: нужно ручное подтверждение")
                session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                                      action="gate_sig_guard", details=details))
                await session.commit()
            log.warning("gate: пост #%s — предохранитель подписей, оставлен ручным", post_id)
            return
        await _approve(post_id, details)
        return
    if g.verdict == "reject":
        await _reject(post_id, topic_id, g.reason, details)
        return
    # revise: один круг редактора + повторный гейт
    new_final = await _revise_once(post_id, listing, kb, draft, g.notes or [])
    if new_final is None:
        async with session_scope() as session:
            session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                                  action="gate_verdict", details=details))
            await session.commit()
        return
    try:
        g2 = await _run_gate(t, listing, kb, new_final)
    except Exception as exc:  # noqa: BLE001
        log.warning("gate: пост #%s — повторный гейт не удался: %s", post_id, exc)
        return
    details2 = {"verdict": g2.verdict, "notes": g2.notes or [],
                "reason": g2.reason or "", "mode": mode, "round": 2}
    log.info("gate: пост #%s круг 2 — %s", post_id, g2.verdict.upper())
    if g2.verdict == "approve" and not _sig_guard_hit(new_final):
        await _approve(post_id, details2)
    elif g2.verdict == "reject":
        await _reject(post_id, topic_id, g2.reason, details2)
    else:
        async with session_scope() as session:
            session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                                  action="gate_verdict", details=details2))
            await session.commit()
        log.info("gate: пост #%s оставлен ручным после круга 2 (%s)",
                 post_id, g2.verdict)