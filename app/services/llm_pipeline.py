"""Этап 3: LLM-классификация и рерайт через OpenRouter.

Путь поста:
    PREFILTERED -> LLM_CLASSIFYING -> CANDIDATE -> REWRITING -> AWAITING_REVIEW
Отказ на классификации -> UNSUITABLE (причина и риски сохраняются).
Любой сбой модели -> NEEDS_MANUAL_REVIEW (пост не теряется молча).
Каждый вызов пишется в llm_calls; стоимость учитывается предохранителями.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import timezone

from sqlalchemy import func, select

from app.config import settings
from app.db.enums import (
    DraftOrigin,
    EventActor,
    LLMCallStatus,
    LLMStage,
    PostStatus,
    PublishJobState,
    PublishMode,
)
from app.db.models import (
    LLMCall,
    Post,
    PostDraftVersion,
    PostEvent,
    PublishJob,
    Source,
    StyleProfile,
    TargetChannel,
)
from app.db.session import session_scope
from app.services import guards
from app.services.llm.openrouter import LLMResponse, OpenRouterError, chat_completion
from app.services.llm.prompts import (
    CLEAN_SYSTEM,
    CLEAN_USER,
    CLEAN_VERSION,
    CLASSIFY_USER,
    CLASSIFY_VERSION,
    DOUBLE_CHECK_SYSTEM,
    DOUBLE_CHECK_USER,
    DOUBLE_CHECK_VERSION,
    REWRITE_SYSTEM_TEMPLATE,
    REWRITE_USER,
    REWRITE_VERSION,
    REVISE_SYSTEM,
    REVISE_USER,
    REVISE_VERSION,
    build_classify_prompt,
    build_style_instructions,
)
from app.services.llm.schemas import (
    ClassifyResult,
    DoubleCheckResult,
    LLMParseError,
    RewriteResult,
)
from app.services.prefilter import run_prefilter
from app.services.publishing import create_publish_job
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_now


log = logging.getLogger(__name__)

TEXT_LIMIT = 6000  #Very длинные исходники усекаем до вызова модели


async def _get_status(post_id: int) -> PostStatus | None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        return post.status if post is not None else None


async def _model_for(key: str) -> str:
    async with session_scope() as session:
        return str(await get_setting(session, key))


_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]")


def _has_cjk(text: str | None) -> bool:
    return bool(text and _CJK_RE.search(text))


async def _translate_to_russian(text: str, model: str, providers) -> tuple[str | None, "LLMResponse | None"]:
    """Дешёвый перевод причины на русский, если модель ответила иероглифами."""
    messages = [
        {"role": "system", "content": "Ты — переводчик. Переведи текст на русский язык. Ответь только переводом, без пояснений и кавычек."},
        {"role": "user", "content": text},
    ]
    try:
        resp = await chat_completion(messages, model, max_tokens=300, temperature=0.0, provider=providers)
        translated = (resp.content or "").strip()
        if translated and not _has_cjk(translated):
            return translated, resp
    except Exception:  # noqa: BLE001
        log.warning("не удалось перевести причину классификации на русский")
    return None, None


async def _providers_for(key: str) -> dict | None:
    async with session_scope() as session:
        return await get_providers(session, key)


async def _get_default_profile(session) -> StyleProfile:
    """Профиль 'default'; создаётся автоматически при первом обращении."""
    profile = (
        await session.execute(select(StyleProfile).where(StyleProfile.name == "default"))
    ).scalar_one_or_none()
    if profile is None:
        profile = StyleProfile(name="default", preserve_source_tone=True, version=1, is_active=True)
        session.add(profile)
        await session.flush()
        log.info("создан стилевой профиль по умолчанию (сохранять тон исходника)")
    return profile


async def _profile_for_post(session, post) -> StyleProfile:
    """Стиль целевого канала, если пост привязан; иначе профиль по умолчанию."""
    if post.target_channel_id is not None:
        channel = await session.get(TargetChannel, post.target_channel_id)
        if channel is not None and channel.style_profile_id is not None:
            profile = await session.get(StyleProfile, channel.style_profile_id)
            if profile is not None:
                return profile
    return await _get_default_profile(session)


def _make_call_row(post_id, stage, model, prompt_version, messages, resp, parsed, status, error) -> LLMCall:
    return LLMCall(
        post_id=post_id,
        stage=stage,
        provider="openrouter",
        model=model,
        prompt_version=prompt_version,
        request={"messages": messages},
        response={"content": resp.content, "parsed": parsed, "provider": resp.provider} if resp is not None else None,
        status=status,
        error=error,
        input_tokens=resp.input_tokens if resp is not None else None,
        output_tokens=resp.output_tokens if resp is not None else None,
        cost_usd=resp.cost_usd if resp is not None else None,
        latency_ms=resp.latency_ms if resp is not None else None,
    )


async def _call_and_parse(messages, model, max_tokens, temperature, schema, provider=None):
    """Вызов модели + парсинг. Возвращает (ответ, результат, статус, текст ошибки)."""
    resp: LLMResponse | None = None
    result = None
    call_status = LLMCallStatus.OK
    error_text: str | None = None
    try:
        resp = await chat_completion(messages, model, max_tokens, temperature=temperature, provider=provider)
        result = schema.from_response(resp.content)
    except OpenRouterError as exc:
        call_status, error_text = LLMCallStatus.ERROR, str(exc)
    except LLMParseError as exc:
        call_status = LLMCallStatus.PARSE_ERROR
        if resp is not None and resp.finish_reason == "length":
            error_text = f"ответ модели обрезан лимитом токенов: {exc}"
        else:
            error_text = str(exc)
    except Exception as exc:  # noqa: BLE001
        call_status, error_text = LLMCallStatus.ERROR, f"{exc.__class__.__name__}: {exc}"
    return resp, result, call_status, error_text


async def classify_post(post_id: int) -> None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status not in (PostStatus.PREFILTERED, PostStatus.LLM_CLASSIFYING):
            return
        original_text = (post.original_text or "")[:TEXT_LIMIT]
        source = await session.get(Source, post.source_id)
        channel = None
        if post.target_channel_id is not None:
            channel = await session.get(TargetChannel, post.target_channel_id)
        relevance = source.relevance if source is not None else None
        post.status = PostStatus.LLM_CLASSIFYING
        await session.commit()

    async with session_scope() as session:
        verbose = bool(await get_setting(session, Keys.CLASSIFY_VERBOSE))
    model = await _model_for(Keys.CLASSIFY_MODEL)
    providers = await _providers_for(Keys.CLASSIFY_PROVIDERS)
    system_prompt = build_classify_prompt(
        channel_title=channel.title if channel is not None else None,
        channel_description=channel.description if channel is not None else None,
        relevance=relevance,
        verbose=verbose,
    )
    system_prompt = build_classify_prompt(
        channel_title=channel.title if channel is not None else None,
        channel_description=channel.description if channel is not None else None,
        relevance=relevance,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": CLASSIFY_USER.format(text=original_text)},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_classify_max_tokens, temperature=0.2, schema=ClassifyResult
    )
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)

    # Языковой барьер: если причина пришла иероглифами — переводим тем же дешёвым вызовом
    translate_resp = None
    if result is not None and _has_cjk(result.reason):
        translated, translate_resp = await _translate_to_russian(result.reason, model, providers)
        if translated:
            result.reason = translated
    if result is not None and result.risks:
        result.risks = [r for r in result.risks if not _has_cjk(r)]
    if translate_resp is not None and translate_resp.cost_usd:
        await guards.add_llm_cost(translate_resp.cost_usd)

    async with session_scope() as session:
        session.add(_make_call_row(
            post_id, LLMStage.CLASSIFY, model, CLASSIFY_VERSION, messages,
            resp, asdict(result) if result is not None else None, call_status, error_text,
        ))
        if translate_resp is not None:
            session.add(LLMCall(
                post_id=post_id, stage=LLMStage.CLASSIFY, provider="openrouter", model=model,
                prompt_version="translate-v1", request=None,
                response={"content": translate_resp.content}, status=LLMCallStatus.OK,
                input_tokens=translate_resp.input_tokens, output_tokens=translate_resp.output_tokens,
                cost_usd=translate_resp.cost_usd, latency_ms=translate_resp.latency_ms,
            ))
        post = await session.get(Post, post_id)
        if post is None:
            await session.commit()
            return
        if call_status is not LLMCallStatus.OK or result is None:
            post.status = PostStatus.NEEDS_MANUAL_REVIEW
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="llm_failed",
                from_status=PostStatus.LLM_CLASSIFYING.value, to_status=PostStatus.NEEDS_MANUAL_REVIEW.value,
                details={"stage": "classify", "error": error_text},
            ))
            log.warning("пост %s: ошибка классификации -> NEEDS_MANUAL_REVIEW (%s)", post_id, error_text)
        elif result.suitable:
            post.status = PostStatus.CANDIDATE
            post.score = result.score
            post.verdict_reason = result.reason
            post.risks = result.risks
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.LLM, action="classified",
                from_status=PostStatus.LLM_CLASSIFYING.value, to_status=PostStatus.CANDIDATE.value,
                details={"score": result.score, "reason": result.reason, "risks": result.risks},
            ))
            log.info("пост %s: классификация -> CANDIDATE (оценка %.1f)", post_id, result.score)
        else:
            post.status = PostStatus.UNSUITABLE
            post.score = result.score
            post.verdict_reason = result.reason
            post.risks = result.risks
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.LLM, action="llm_rejected",
                from_status=PostStatus.LLM_CLASSIFYING.value, to_status=PostStatus.UNSUITABLE.value,
                details={"score": result.score, "reason": result.reason},
            ))
            log.info("пост %s: классификация -> UNSUITABLE (%s)", post_id, (result.reason or "")[:120])
        await session.commit()
        

async def rewrite_post(post_id: int) -> None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status not in (PostStatus.CANDIDATE, PostStatus.REWRITING):
            return
        channel = None
        if post.target_channel_id is not None:
            channel = await session.get(TargetChannel, post.target_channel_id)

        # Канал без авторерайта: черновик = оригинал, модель не вызываем
        if channel is not None and not channel.rewrite_enabled:
            original_text = post.original_text or ""
            from_status = post.status.value
            post.draft_text = original_text
            post.draft_version += 1
            post.status = PostStatus.AWAITING_REVIEW
            session.add(PostDraftVersion(
                post_id=post_id, version=post.draft_version, text=original_text,
                origin=DraftOrigin.ORIGINAL,
            ))
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="rewrite_skipped",
                from_status=from_status, to_status=PostStatus.AWAITING_REVIEW.value,
                details={"reason": "rewrite_disabled"},
            ))
            await session.commit()
            log.info("пост %s: рерайт отключён у канала — черновик = оригинал (v%d)", post_id, post.draft_version)
            await _autopilot_step(post_id)
            return
        profile = await _profile_for_post(session, post)
        profile_id = profile.id
        style_instructions = build_style_instructions(profile)
        original_text = (post.original_text or "")[:TEXT_LIMIT]
        post.status = PostStatus.REWRITING
        await session.commit()

    model = await _model_for(Keys.REWRITE_MODEL)
    providers = await _providers_for(Keys.REWRITE_PROVIDERS)
    system_prompt = REWRITE_SYSTEM_TEMPLATE.format(style_instructions=style_instructions)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": REWRITE_USER.format(text=original_text)},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_rewrite_max_tokens, temperature=0.4,
        schema=RewriteResult, provider=providers,
    )
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)

    succeeded = call_status is LLMCallStatus.OK and result is not None

    async with session_scope() as session:
        call_row = _make_call_row(
            post_id, LLMStage.REWRITE, model, REWRITE_VERSION, messages,
            resp, asdict(result) if result is not None else None, call_status, error_text,
        )
        session.add(call_row)
        await session.flush()  # id вызова — для связи версии черновика

        post = await session.get(Post, post_id)
        if post is None:
            await session.commit()
            return
        if not succeeded:
            post.status = PostStatus.NEEDS_MANUAL_REVIEW
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="llm_failed",
                from_status=PostStatus.REWRITING.value, to_status=PostStatus.NEEDS_MANUAL_REVIEW.value,
                details={"stage": "rewrite", "error": error_text},
            ))
            log.warning("пост %s: ошибка рерайта -> NEEDS_MANUAL_REVIEW (%s)", post_id, error_text)
        else:
            post.draft_text = result.draft
            post.draft_version += 1
            post.style_profile_id = profile_id
            post.status = PostStatus.AWAITING_REVIEW
            session.add(PostDraftVersion(
                post_id=post_id,
                version=post.draft_version,
                text=result.draft,
                origin=DraftOrigin.LLM_REWRITE,
                llm_call_id=call_row.id,
            ))
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.LLM, action="sent_to_review",
                from_status=PostStatus.REWRITING.value, to_status=PostStatus.AWAITING_REVIEW.value,
                details={"draft_version": post.draft_version, "warnings": result.warnings},
            ))
            log.info("пост %s: рерайт -> AWAITING_REVIEW (черновик v%d)", post_id, post.draft_version)
        await session.commit()

    if succeeded:
        await guards.inc_candidates()
        await _autopilot_step(post_id)


async def advance_post(post_id: int) -> None:
    """Продвигает пост по конвейеру до ожидающего/терминального состояния."""
    try:
        status = await _get_status(post_id)
        if status is None:
            return

        if status is PostStatus.NEW:
            await run_prefilter(post_id)
            status = await _get_status(post_id)
            if status is None:
                return

        if status in (PostStatus.PREFILTERED, PostStatus.LLM_CLASSIFYING):
            if not settings.openrouter_api_key:
                log.warning("OPENROUTER_API_KEY не задан — пост %s ждёт в %s", post_id, status.value)
                return
            if not await guards.candidates_cap_allows():
                log.info("пост %s: достигнут лимит кандидатов/день — ждёт", post_id)
                return
            if not await guards.budget_allows():
                log.warning("пост %s: достигнут бюджет LLM/день — ждёт", post_id)
                return
            await classify_post(post_id)
            status = await _get_status(post_id)

        if status in (PostStatus.CANDIDATE, PostStatus.REWRITING):
            if not settings.openrouter_api_key:
                return
            if not await guards.budget_allows():
                log.warning("пост %s: достигнут бюджет LLM/день — ждёт", post_id)
                return
            await rewrite_post(post_id)
    except Exception:  # noqa: BLE001 — пост не теряется, подхватится ресканом
        log.exception("сбой обработки поста %s", post_id)



async def revise_draft(post_id: int, comment: str) -> tuple[bool, str]:
    """Правка ИИ: замечание владельца + текущий черновик -> новая версия."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return False, "пост не найден"
        if post.status not in (PostStatus.REVISION, PostStatus.AWAITING_REVIEW):
            return False, f"правка недоступна в статусе {post.status.value}"
        draft = post.draft_text or ""
        if not draft:
            return False, "у поста ещё нет черновика"

    if not settings.openrouter_api_key:
        return False, "OPENROUTER_API_KEY не задан"
    if not await guards.budget_allows():
        return False, "бюджет LLM на сегодня исчерпан — попробуйте завтра"

    model = await _model_for(Keys.REVISION_MODEL)
    providers = await _providers_for(Keys.REVISION_PROVIDERS)
    messages = [
        {"role": "system", "content": REVISE_SYSTEM},
        {"role": "user", "content": REVISE_USER.format(draft=draft[:TEXT_LIMIT], comment=comment[:2000])},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_rewrite_max_tokens, temperature=0.7,
        schema=RewriteResult, provider=providers,
    )
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)

    async with session_scope() as session:
        call_row = _make_call_row(
            post_id, LLMStage.REVISION, model, REVISE_VERSION, messages,
            resp, asdict(result) if result is not None else None, call_status, error_text,
        )
        session.add(call_row)
        await session.flush()

        post = await session.get(Post, post_id)
        if post is None:
            await session.commit()
            return False, "пост не найден"

        if call_status is not LLMCallStatus.OK or result is None:
            # Возвращаем в ожидание ревью: владелец рядом, пост не теряется
            post.status = PostStatus.AWAITING_REVIEW
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="revision_failed",
                from_status=PostStatus.REVISION.value, to_status=PostStatus.AWAITING_REVIEW.value,
                details={"error": error_text},
            ))
            await session.commit()
            return False, f"модель не смогла внести правку: {(error_text or '')[:200]}"

        post.draft_text = result.draft
        post.draft_version += 1
        post.status = PostStatus.AWAITING_REVIEW
        session.add(PostDraftVersion(
            post_id=post_id, version=post.draft_version, text=result.draft,
            origin=DraftOrigin.LLM_REVISION, llm_call_id=call_row.id,
        ))
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.LLM, action="revised",
            from_status=PostStatus.REVISION.value, to_status=PostStatus.AWAITING_REVIEW.value,
            details={"draft_version": post.draft_version, "warnings": result.warnings},
        ))
        # карточка будет обновлена на месте — повторная рассылка не нужна
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="card_sent",
            details={"draft_version": post.draft_version},
        ))
        await session.commit()

    log.info("пост %s: правка ИИ -> черновик v%d", post_id, post.draft_version)
    return True, f"правка внесена — черновик v{post.draft_version}"


async def _autopilot_step(post_id: int) -> None:
    try:
        async with session_scope() as session:
            post = await session.get(Post, post_id)
            if post is None or post.status is not PostStatus.AWAITING_REVIEW:
                return
            channel = await session.get(TargetChannel, post.target_channel_id) \
                if post.target_channel_id else None
            if channel is None or not channel.autopilot:
                return
            min_score = channel.autopilot_min_score or settings.autopilot_min_score
            confident = (post.score or 0.0) >= min_score
            review_if_uncertain = channel.review_if_uncertain
            double_check = channel.double_check
        if not confident:
            if review_if_uncertain:
                log.info("пост %s: автопилот не уверен — оставлен в ревью", post_id)
                return
            await _autopilot_reject(post_id, "автопилот: ниже порога уверенности")
            return
        await _ensure_clean_draft(post_id)
        if double_check:
            approve, note = await _run_double_check(post_id)
            if not approve:
                await _set_double_check_review(post_id, note or "нет вердикта — проверить вручную")
                return
        await _autopilot_publish(post_id)
    except Exception:  # noqa: BLE001
        log.exception("сбой автопилота поста %s", post_id)


async def _autopilot_reject(post_id: int, reason: str) -> None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        post.status = PostStatus.UNSUITABLE
        post.reject_reason = reason
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM, action="autopilot_rejected",
                              from_status=PostStatus.AWAITING_REVIEW.value,
                              to_status=PostStatus.UNSUITABLE.value, details={"reason": reason}))
        await session.commit()
    log.info("пост %s: автопилот отклонил (%s)", post_id, reason)


async def _ensure_clean_draft(post_id: int) -> None:
    """Для каналов без рерайта: дешёвым вызовом убрать чужие подписи."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        channel = await session.get(TargetChannel, post.target_channel_id) \
            if post.target_channel_id else None
        if channel is not None and channel.rewrite_enabled:
            return  # рерайт уже чистит подписи (правило 7)
        text = post.draft_text or post.original_text or ""
    model = await _model_for(Keys.PREFILTER_MODEL)
    messages = [
        {"role": "system", "content": CLEAN_SYSTEM},
        {"role": "user", "content": CLEAN_USER.format(text=text[:TEXT_LIMIT])},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_rewrite_max_tokens, temperature=0.0, schema=RewriteResult)
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)
    async with session_scope() as session:
        session.add(_make_call_row(post_id, LLMStage.REWRITE, model, CLEAN_VERSION, messages,
                                   resp, asdict(result) if result else None, call_status, error_text))
        post = await session.get(Post, post_id)
        if post is None:
            await session.commit(); return
        if call_status is LLMCallStatus.OK and result is not None and result.draft:
            post.draft_text = result.draft
            post.draft_version += 1
            session.add(PostDraftVersion(post_id=post_id, version=post.draft_version,
                                         text=result.draft, origin=DraftOrigin.LLM_REWRITE))
        await session.commit()


async def _run_double_check(post_id: int) -> tuple[bool, str]:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return False, "пост не найден"
        channel = await session.get(TargetChannel, post.target_channel_id) \
            if post.target_channel_id else None
        draft = post.draft_text or post.original_text or ""
        verdict = post.verdict_reason or ""
        score = post.score
        title = channel.title if channel else "канал"
        desc = channel.description if channel else ""
        model = (await get_setting(session, Keys.DOUBLE_CHECK_MODEL)) or settings.effective_revision_model
    messages = [
        {"role": "system", "content": DOUBLE_CHECK_SYSTEM.format(channel_title=title)},
        {"role": "user", "content": DOUBLE_CHECK_USER.format(
            channel_description=desc,
            score=f"{score:.1f}" if score is not None else "—",
            verdict=verdict or "нет",
            draft=draft[:TEXT_LIMIT])},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_rewrite_max_tokens, temperature=0.1, schema=DoubleCheckResult)
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)
    async with session_scope() as session:
        session.add(_make_call_row(post_id, LLMStage.REVISION, model, DOUBLE_CHECK_VERSION, messages,
                                   resp, asdict(result) if result else None, call_status, error_text))
        await session.commit()
    if result is None:
        return False, "двойная проверка не дала ответа — нужна ручная проверка"
    return result.approve, result.note


async def _set_double_check_review(post_id: int, note: str) -> None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        post.status = PostStatus.DOUBLE_CHECK_REVIEW
        post.double_check_note = note
        session.add(PostEvent(post_id=post_id, actor=EventActor.LLM, action="double_check_review",
                              from_status=PostStatus.AWAITING_REVIEW.value,
                              to_status=PostStatus.DOUBLE_CHECK_REVIEW.value, details={"note": note}))
        await session.commit()
    log.info("пост %s: двойная проверка вернула на ревью (%s)", post_id, note[:120])


async def _autopilot_publish(post_id: int) -> None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        channel = await session.get(TargetChannel, post.target_channel_id) \
            if post.target_channel_id else None
        if channel is None:
            return
        day_start = owner_now().replace(hour=0, minute=0, second=0, microsecond=0)
        published_today = await session.scalar(
            select(func.count()).select_from(PublishJob).where(
                PublishJob.target_channel_id == channel.id,
                PublishJob.state == PublishJobState.DONE,
                PublishJob.published_at >= day_start.astimezone(timezone.utc),
            ))
        if published_today is not None and published_today >= channel.daily_limit:
            post.status = PostStatus.AWAITING_REVIEW
            post.reject_reason = "автопилот: дневной лимит публикаций исчерпан"
            session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM, action="autopilot_limit",
                                  to_status=PostStatus.AWAITING_REVIEW.value,
                                  details={"reason": "daily_limit"}))
            await session.commit()
            log.info("пост %s: автопилот остановлен лимитом — в ревью", post_id)
            return
        post.status = PostStatus.APPROVED
        post.autopilot = True
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM, action="autopilot_approved",
                              to_status=PostStatus.APPROVED.value, details={"score": post.score}))
        await session.commit()
    ok, msg = await create_publish_job(post_id, PublishMode.NOW)
    log.info("пост %s: автопилот -> публикация (%s)", post_id, msg)