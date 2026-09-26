"""Этап 3: LLM-классификация и рерайт через OpenRouter.

Путь поста:
    PREFILTERED -> LLM_CLASSIFYING -> CANDIDATE -> REWRITING -> AWAITING_REVIEW
Отказ на классификации -> UNSUITABLE (причина и риски сохраняются).
Любой сбой модели -> NEEDS_MANUAL_REVIEW (пост не теряется молча).
Каждый вызов пишется в llm_calls; стоимость учитывается предохранителями.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
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
    MediaType,
)
from app.db.models import (
    LLMCall,
    MediaItem,
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
    AGGREGATE_SYSTEM,
    AGGREGATE_USER,
    AGGREGATE_VERSION,
    CLASSIFY_USER,
    CLASSIFY_VERSION,
    REWRITE_SYSTEM_TEMPLATE,
    REWRITE_USER,
    REWRITE_VERSION,
    REVISE_SYSTEM,
    REVISE_USER,
    REVISE_VERSION,
    CLEAN_SYSTEM,
    CLEAN_USER,
    CLEAN_VERSION,
    DOUBLE_CHECK_USER,
    DOUBLE_CHECK_VERSION,
    build_classify_prompt,
    build_style_instructions,
    build_double_check_prompt,
    LANGUAGE_RULES,
    with_language_rules,
)
from app.services.llm.schemas import (
    ClassifyResult,
    CleanPlanResult,
    DoubleCheckResult,
    LLMParseError,
    RewriteResult,
    is_provider_safety_reply,
)
from app.services.prefilter import run_prefilter
from app.services.publishing import create_publish_job
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_now
from app.services.dedup import run_semantic_dedup


log = logging.getLogger(__name__)

TEXT_LIMIT = 6000  #Очень длинные исходники усекаем до вызова модели
REASONING_HARD_CAP = 4000  # жёсткий потолок бюджета рассуждений: защита от опечаток в настройках и от зациклов

# Голые url вне markdown-ссылок: удаляются кодом, а не моделью
_BARE_URL_RE = re.compile(r"(?<!\]\()(?<!\()(?:https?://|t\.me/|telegram\.me/)[^\s)\]]+")

_MD_LINK_FULL_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_TG_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[\w+/\-]+", re.I)
_ANY_LINK_RE = re.compile(
    r"https?://\S+|(?:t\.me|telegram\.me)/[\w+/\-]+|@[A-Za-z0-9_]{4,}", re.I)
_CTA_RE = re.compile(r"подпис|subscribe|наш канал|наш телеграм|читайте нас|смотрите нас", re.I)
_SOURCE_LINE_RE = re.compile(r"^\s*\W{0,3}\s*источник\s*[:—-]", re.I)

_HASHTAG_LINE_RE = re.compile(r"^\s*(?:#[\wа-яёА-ЯЁ\-]+)(?:\s+#[\wа-яёА-ЯЁ\-]+)*\s*$")
_HASHTAG_TAIL_RE = re.compile(r"(?:\s+#[\wа-яёА-ЯЁ\-]+)+\s*$")
_TEASER_WORD_RE = re.compile(
    r"подробнее|подробней|читайте|смотрите|слушайте|обсуд|подпис|тут|здесь|чат|буст|boost|"
    r"наш канал|наш дзен|наш zen|переходи|присоедин", re.I)
# Личные/UGC-площадки и трафик-ссылки: dzen, pikabu, vk, ok, youtube-КАНАЛ, t.me-чат/boost/invite…
# (youtube.com/watch и официальные сайты сюда НЕ входят — это контент, а не декор)
_UGC_HOST_RE = re.compile(
    r"(?<![a-z0-9\-])(?:dzen\.ru|zen\.yandex\.[a-z]{2,3}|pikabu\.ru|vk\.com|vk\.ru|vkontakte\.ru|"
    r"ok\.ru|odnoklassniki\.ru|twitter\.com|x\.com|instagram\.com|tiktok\.com|"
    r"livejournal\.com|t\.me/(?:\+|boost/|joinchat/)|youtube\.com/(?:@|channel/|c/)|"
    r"youtu\.be/(?:@|channel/)|drive\.google\.com|docs\.google\.com)", re.I)
_DECOR_SUSPECT_RE = re.compile(
    r"узнать|больше|подроб|читайте|смотрите|слушайте|подпис|наш|перейти|обсуд|чат|буст|boost|"
    r"канал|дзен|zen|pikabu|vk\.|ok\.ru|youtube|t\.me|https?://|\[", re.I)


def _line_urls(line: str) -> list:
    out = [m.group(2) for m in _MD_LINK_FULL_RE.finditer(line)]
    out += _BARE_URL_RE.findall(line)
    return [u for u in out if u]


def _is_teaser_line(line: str) -> bool:
    """Строка-тизер: все ссылки ведут на личные/UGC-страницы, остальной текст — обвязка."""
    urls = _line_urls(line)
    if not urls or not all(_UGC_HOST_RE.search(u) for u in urls):
        return False
    leftover = _MD_LINK_FULL_RE.sub("", line)
    leftover = _BARE_URL_RE.sub("", leftover)
    plain = " ".join(re.sub(r"[^\w\s]", " ", leftover).split())
    if len(plain) <= 20:
        return True
    return bool(_TEASER_WORD_RE.search(plain)) and len(plain) <= 60


def _strip_source_decor(text: str) -> str:
    """Убирает хэштеги источника и строки-тизеры; официальный контент не трогает."""
    lines = [ln for ln in text.split("\n")
             if not _HASHTAG_LINE_RE.match(ln) and not _is_teaser_line(ln)]
    res = _HASHTAG_TAIL_RE.sub("", "\n".join(lines)).rstrip()
    return re.sub(r"\n{3,}", "\n\n", res).strip()


async def _strip_decor_enabled() -> bool:
    async with session_scope() as session:
        v = await get_setting(session, Keys.PUBLISH_STRIP_SOURCE_DECOR)
    return True if v is None else bool(int(v))
    

def _plain_len(s: str) -> int:
    """Длина строки без эмодзи/скобок/пунктуации — для оценки «строка состоит из ссылки»."""
    return len(re.sub(r"[^\w\s]", "", s or "", flags=re.UNICODE).strip())


def _signature_lines(lines: list, source_username: str | None = None) -> list:
    """Детерминированный поиск строк-подписей источника (без вызова модели).

    Правила (достаточно одного):
      1) ссылка или упоминание канала САМОГО источника;
      2) строка «Источник: …»;
      3) CTA-слова вместе со ссылкой/упоминанием канала;
      4) CTA-строка в хвосте поста длиной <= 60 символов;
      5) строка-ссылка в хвосте поста, где после удаления ссылок почти ничего не остаётся
         (типовой футер «🎬[Название канала](https://t.me/…)»).
    Возвращает номера строк (1-based).
    """
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    tail = set(nonempty[-3:]) if nonempty else set()
    uname = (source_username or "").strip().lstrip("@").lower()
    out = []
    for i in nonempty:
        s = lines[i].strip()
        if _HASHTAG_LINE_RE.match(s) or _is_teaser_line(s):
            out.append(i + 1)
            continue
        low = s.lower()
        has_link = bool(_ANY_LINK_RE.search(s))
        if uname and (f"t.me/{uname}" in low or f"@{uname}" in low):
            out.append(i + 1)
            continue
        if _SOURCE_LINE_RE.match(s):
            out.append(i + 1)
            continue
        if _CTA_RE.search(s) and (has_link or (i in tail and len(s) <= 60)):
            out.append(i + 1)
            continue
        if i in tail and _ANY_LINK_RE.search(s):
            rest = _BARE_URL_RE.sub("", _MD_LINK_FULL_RE.sub("", s))
            if _plain_len(rest) <= 3:
                out.append(i + 1)
    return out

async def _get_status(post_id: int) -> PostStatus | None:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        return post.status if post is not None else None


async def _model_for(key: str) -> str:
    async with session_scope() as session:
        return str(await get_setting(session, key))


async def _model_for_post(post, key: str) -> str:
    """Для чувствительных постов — отдельная модель (если задана), иначе обычная."""
    if post is not None and getattr(post, "sensitive", False):
        async with session_scope() as session:
            sm = str(await get_setting(session, Keys.LLM_SENSITIVE_MODEL) or "").strip()
        if sm:
            return sm
    return await _model_for(key)


async def _response_lang() -> str:
    """Язык строковых значений ответов моделей: Russian или English."""
    async with session_scope() as session:
        v = str(await get_setting(session, Keys.LLM_RESPONSE_LANG) or "ru").strip().lower()
    return "Russian" if v in ("ru", "rus", "russian") else "English"


async def _response_lang() -> str:
    """Язык строковых значений ответов моделей: Russian или English."""
    async with session_scope() as session:
        v = str(await get_setting(session, Keys.LLM_RESPONSE_LANG) or "ru").strip().lower()
    return "Russian" if v in ("ru", "rus", "russian") else "English"


async def _media_hint(post_id: int) -> str | None:
    async with session_scope() as session:
        rows = (await session.execute(
            select(MediaItem).where(MediaItem.post_id == post_id)
        )).scalars().all()
    n_video = sum(1 for m in rows if m.media_type is MediaType.VIDEO)
    n_photo = sum(1 for m in rows if m.media_type is MediaType.PHOTO)
    parts = []
    if n_video:
        parts.append(f"{n_video} видео")
    if n_photo:
        parts.append(f"{n_photo} фото")
    return " + ".join(parts) or None


_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff]")


def _has_cjk(text: str | None) -> bool:
    return bool(text and _CJK_RE.search(text))


def _script_kind(s: str) -> str:
    """Преобладающая графика текста: cyr | lat | none."""
    cyr = sum(1 for ch in (s or "") if "Ѐ" <= ch <= "ӿ")
    lat = sum(1 for ch in (s or "") if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    if not cyr and not lat:
        return "none"
    return "cyr" if cyr >= lat else "lat"


def _canonical_script_mismatch(source: str, canonical: str) -> bool:
    return _script_kind(source) == "cyr" and _script_kind(canonical) == "lat"


def _canonical_reminder(source: str) -> str:
    script = "Cyrillic" if _script_kind(source) == "cyr" else "Latin"
    return (f"CRITICAL: the \"canonical\" field MUST use the same script as the source text: "
            f"{script}. Copy names, titles and the action verb exactly as they appear in the "
            f"source; transliteration is FORBIDDEN. Answer JSON only.")


async def _translate_to_russian(text: str, model: str, providers) -> tuple[str | None, "LLMResponse | None"]:
    """Дешёвый перевод причины на русский, если модель ответила иероглифами."""
    messages = [
        {"role": "system", "content": "Ты — переводчик. Переведи текст на русский язык. Ответь только переводом, без пояснений и кавычек."},
        {"role": "user", "content": text},
    ]
    try:
        resp = await chat_completion(messages, model, max_tokens=300, temperature=0.0, provider=providers,
                                     reasoning_max_tokens=settings.llm_reasoning_small)
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


_LOOPING_MODELS: set = set()


def _is_reasoning_loop(resp, total_cap: int) -> bool:
    """Дегенеративный зацикл: выход съел ~весь лимит, финального ответа нет."""
    if resp is None or getattr(resp, "finish_reason", None) != "length":
        return False
    if (resp.content or "").strip():
        return False
    out = resp.output_tokens or 0
    return bool(total_cap) and out >= int(total_cap * 0.9)


async def _call_and_parse(messages, model, max_tokens, temperature, schema, provider=None,
                          reasoning_max_tokens: int | None = None):
    """Вызов модели + парсинг. Возвращает (ответ, результат, статус, текст ошибки)."""
    resp: LLMResponse | None = None
    result = None
    call_status = LLMCallStatus.OK
    error_text: str | None = None
    try:
        # max_tokens = лимит финального ответа стадии + бюджет рассуждений:
        # рассуждения тарифицируются внутри max_tokens, ответ не должен голодать
        reason_cap = min(reasoning_max_tokens or 0, REASONING_HARD_CAP)
        if (reasoning_max_tokens or 0) > REASONING_HARD_CAP:
            log.warning("llm: бюджет рассуждений %d урезан до %d (защита от обрыва и расхода)",
                        reasoning_max_tokens, REASONING_HARD_CAP)
        total_cap = max_tokens + reason_cap
        resp = await chat_completion(messages, model, total_cap,
                                     temperature=temperature,
                                     provider=provider, reasoning_max_tokens=reason_cap)
        if _is_reasoning_loop(resp, total_cap):
            _LOOPING_MODELS.add(model)
            raise LLMParseError("reasoning loop: модель зациклилась на повторе и исчерпала лимит без ответа")
        result = schema.from_response(resp.content)
    except OpenRouterError as exc:
        call_status, error_text = LLMCallStatus.ERROR, str(exc)
    except LLMParseError as exc:
        call_status = LLMCallStatus.PARSE_ERROR
        if resp is not None and resp.finish_reason == "length":
            detail = ("рассуждения исчерпали лимит, финальный ответ пуст"
                      if not (resp.content or "").strip() else "финальный ответ обрезан лимитом")
            error_text = f"лимит токенов исчерпан ({detail}): {exc}"
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

    media_hint = await _media_hint(post_id)

    async with session_scope() as session:
        verbose = bool(await get_setting(session, Keys.CLASSIFY_VERBOSE))
    model = await _model_for_post(post, Keys.CLASSIFY_MODEL)
    providers = await _providers_for(Keys.CLASSIFY_PROVIDERS)
    system_prompt = build_classify_prompt(
        channel_title=channel.title if channel is not None else None,
        channel_description=channel.description if channel is not None else None,
        relevance=relevance,
        verbose=verbose,
        media_hint=media_hint,
        source_note=source.llm_instructions if source is not None else None,
        source_username=source.username if source is not None else None,
        source_title=source.title if source is not None else None,
        channel_note=channel.llm_instructions if channel is not None else None,
        response_lang=await _response_lang(),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": CLASSIFY_USER.format(text=original_text)},
    ]
    resp = result = None
    call_status, error_text = LLMCallStatus.OK, None
    for attempt in (1, 2):
        reason_budget = (settings.llm_reasoning_max_tokens if attempt == 1
                         else min(settings.llm_reasoning_max_tokens, 1200))  # повтор дешевле
        model, resp, result, call_status, error_text = await _call_with_fallback(
            messages, model, settings.llm_classify_max_tokens, 0.2, ClassifyResult,
            None, reason_budget,
        )
        if result is not None and call_status is LLMCallStatus.OK:
            # Слабая модель могла транслитерировать канон: один повтор с напоминанием
            if _canonical_script_mismatch(original_text, result.canonical or "") and attempt == 1:
                messages.append({"role": "system", "content": _canonical_reminder(original_text)})
                async with session_scope() as session:
                    session.add(PostEvent(
                        post_id=post_id, actor=EventActor.SYSTEM, action="canonical_lang_retry",
                        details={"attempt": attempt}))
                    await session.commit()
                log.warning("пост %s: canonical пришёл не в той графике — повтор с напоминанием",
                            post_id)
                continue
            break
        if attempt == 1:
            async with session_scope() as session:
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="llm_retry",
                    details={"stage": "classify", "attempt": attempt,
                             "status": call_status.value if call_status else None,
                             "error": error_text}))
                await session.commit()
            log.warning("пост %s: классификация не удалась (%s) — повтор через 60 сек",
                        post_id, error_text)
            await asyncio.sleep(60)

    if result is not None and _canonical_script_mismatch(original_text, result.canonical or ""):
        async with session_scope() as session:
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="canonical_lang_mismatch",
                details={"canonical": (result.canonical or "")[:120]}))
            await session.commit()
        log.warning("пост %s: canonical остался не в той графике после повтора", post_id)

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
            post.canonical_text = result.canonical
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.LLM, action="classified",
                from_status=PostStatus.LLM_CLASSIFYING.value, to_status=PostStatus.CANDIDATE.value,
                details={"score": result.score, "reason": result.reason,
                         "risks": result.risks, "canonical": result.canonical},
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

    await run_semantic_dedup(post_id)
        

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
    system_prompt = REWRITE_SYSTEM_TEMPLATE.format(
        style_instructions=style_instructions,
        language_rules=LANGUAGE_RULES.format(response_lang=await _response_lang()))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": REWRITE_USER.format(text=original_text)},
    ]
    resp = result = None
    call_status, error_text = LLMCallStatus.OK, None
    for attempt in (1, 2):
        resp, result, call_status, error_text = await _call_and_parse(
            messages, model, settings.llm_rewrite_max_tokens, temperature=0.4,
            schema=RewriteResult, provider=providers,
            reasoning_max_tokens=settings.llm_reasoning_rewrite,
        )
        if resp is not None and resp.cost_usd:
            await guards.add_llm_cost(resp.cost_usd)
        if result is not None and call_status is LLMCallStatus.OK:
            break
        if attempt == 1:
            async with session_scope() as session:
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="llm_retry",
                    details={"stage": "rewrite", "attempt": attempt,
                             "status": call_status.value if call_status else None,
                             "error": error_text}))
                await session.commit()
            log.warning("пост %s: рерайт не удался (%s) — повтор через 60 сек", post_id, error_text)
            await asyncio.sleep(60)

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

        async with session_scope() as session:
            post_row = await session.get(Post, post_id)
            src_row = await session.get(Source, post_row.source_id) if post_row is not None else None
        if src_row is not None and src_row.editorial_only:
            return  # сырьё виртуальной редакции: copy-конвейер не трогаем

        if status is PostStatus.NEW:
            await run_prefilter(post_id)
            status = await _get_status(post_id)
            if status is None:
                return

        async with session_scope() as session:
            post_t = await session.get(Post, post_id)
            ch_t = (await session.get(TargetChannel, post_t.target_channel_id)
                    if post_t is not None and post_t.target_channel_id is not None else None)
        if (ch_t is not None and ch_t.no_review
                and status in (PostStatus.PREFILTERED, PostStatus.NEEDS_MEDIA_REVIEW)):
            # Технический канал: тематический фильтр — и только потом пересылка/публикация
            await _aggregate_gate(post_id, ch_t, status)
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
        {"role": "system", "content": REVISE_SYSTEM.format(
            language_rules=LANGUAGE_RULES.format(response_lang=await _response_lang()))},
        {"role": "user", "content": REVISE_USER.format(draft=draft[:TEXT_LIMIT], comment=comment[:2000])},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, settings.llm_rewrite_max_tokens, temperature=0.7,
        schema=RewriteResult, provider=providers,
        reasoning_max_tokens=settings.llm_reasoning_rewrite,
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


_MODEL_SLUG_RE = re.compile(r"^[\w.\-]+/[\w.\-]+(?::[\w.\-]+)?$")


async def _fallback_models() -> list:
    """Запасные модели; некорректные значения (мусор после ручных правок) отбрасываются."""
    async with session_scope() as session:
        raw = await get_setting(session, Keys.LLM_FALLBACK_MODELS)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    out = []
    for x in (raw or []):
        s = str(x).strip()
        if not s:
            continue
        if _MODEL_SLUG_RE.match(s):
            out.append(s)
        else:
            log.warning("llm.fallback_models: пропущено некорректное значение %r", s)
    return out


async def _call_with_fallback(messages, model, max_tokens, temperature, schema,
                              providers, reasoning_max_tokens):
    """Вызов с ротацией: основная модель + llm_fallback_models.

    Ответ модели модерации и непроходимый JSON считаются сбоем маршрутизации —
    пробуем следующую модель. Возвращает (использованная модель, resp, result, status, error).
    """
    base_chain = [model] + [m for m in await _fallback_models() if m != model]
    clean = [m for m in base_chain if m not in _LOOPING_MODELS]
    chain = clean + [m for m in base_chain if m not in clean]   # зацикленные — в конец
    used, resp, result = model, None, None
    call_status, error_text = LLMCallStatus.ERROR, "нет ответа"
    for m in chain:
        used = m
        resp, result, call_status, error_text = await _call_and_parse(
            messages, m, max_tokens, temperature=temperature, schema=schema,
            provider=providers, reasoning_max_tokens=reasoning_max_tokens)
        if resp is not None and resp.cost_usd:
            await guards.add_llm_cost(resp.cost_usd)
        if resp is not None and is_provider_safety_reply(resp.content):
            result = None
            call_status = LLMCallStatus.PARSE_ERROR
            error_text = f"модель {m} вернула ответ модерации: {resp.content[:60]!r}"
            log.warning("llm: %s — ответ модели модерации вместо JSON, пробуем следующую", m)
            continue
        if result is not None and call_status is LLMCallStatus.OK:
            break
    return used, resp, result, call_status, error_text


async def _aggregate_filter(post_id: int, channel) -> tuple[bool | None, float, str]:
    """Дешёвая тематическая фильтрация постов технического канала-агрегатора."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return False, 0.0, "пост не найден"
        text = (post.original_text or "")[:TEXT_LIMIT]
        accept_default = str(await get_setting(session, Keys.AGGREGATE_ACCEPT_DEFAULT))
        reject_default = str(await get_setting(session, Keys.AGGREGATE_REJECT_DEFAULT))
    topic = (channel.description or "").strip() or "тематика канала"
    accept = (channel.aggregate_accept or "").strip() or accept_default
    reject = (channel.aggregate_reject or "").strip() or reject_default
    model = await _model_for_post(post, Keys.CLASSIFY_MODEL)
    providers = await _providers_for(Keys.CLASSIFY_PROVIDERS)
    lang = await _response_lang()
    messages = [
        {"role": "system", "content": AGGREGATE_SYSTEM.format(
            channel_title=(channel.title or channel.username or "канал"),
            topic=topic, accept=accept, reject=reject,
            response_lang=lang,
            language_rules=LANGUAGE_RULES.format(response_lang=lang))},
        {"role": "user", "content": AGGREGATE_USER.format(text=text)},
    ]
    model, resp, result, call_status, error_text = await _call_with_fallback(
        messages, model, 600, 0.1, ClassifyResult, providers, settings.llm_reasoning_small)
    async with session_scope() as session:
        session.add(_make_call_row(post_id, LLMStage.CLASSIFY, model, AGGREGATE_VERSION, messages,
                                   resp, asdict(result) if result else None, call_status, error_text))
        await session.commit()
    if result is None:
        # None = вердикта нет (технический сбой). Это НЕ «не подходит».
        return None, 0.0, f"фильтр агрегатора не дал ответа: {error_text}"
    return bool(result.suitable), float(result.score or 0.0), (result.reason or result.category or "")


async def _aggregate_gate(post_id: int, channel, from_status) -> None:
    """Ворота технического канала: фильтр темы -> пересылка/публикация или отклонение."""
    async with session_scope() as session:
        p = await session.get(Post, post_id)
        if (p is None or p.repost_pending
                or p.status not in (PostStatus.PREFILTERED, PostStatus.NEEDS_MEDIA_REVIEW)):
            log.info("пост %s: ворота агрегатора пропущены (статус %s, repost_pending=%s)",
                     post_id, p.status.value if p is not None else "—",
                     p.repost_pending if p is not None else None)
            return
    ok, score, reason = await _aggregate_filter(post_id, channel)
    if ok is None:
        async with session_scope() as session:
            post = await session.get(Post, post_id)
            if post is None:
                return
            tries = len((await session.execute(
                select(PostEvent.id).where(
                    PostEvent.post_id == post_id,
                    PostEvent.action == "aggregate_no_verdict"))).all())
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="aggregate_no_verdict",
                from_status=post.status.value, to_status=post.status.value,
                details={"reason": reason, "try": tries + 1}))
            if tries + 1 >= 3:
                post.status = PostStatus.NEEDS_MANUAL_REVIEW
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="aggregate_giveup",
                    to_status=PostStatus.NEEDS_MANUAL_REVIEW.value,
                    details={"reason": reason}))
            await session.commit()
        if tries + 1 >= 3:
            log.warning("пост %s: фильтр агрегатора 3 раза без вердикта — ручное ревью", post_id)
        else:
            log.warning("пост %s: фильтр агрегатора без вердикта (%s) — повтор на рескане",
                        post_id, reason)
        return
    threshold = int(channel.aggregate_min_score or 6)
    if not ok or score < threshold:
        async with session_scope() as session:
            post = await session.get(Post, post_id)
            if post is None:
                return
            prev = post.status.value
            post.status = PostStatus.UNSUITABLE
            post.score = score
            post.verdict_reason = f"фильтр агрегатора: {reason or 'вне темы'}"
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.LLM, action="aggregate_rejected",
                from_status=prev, to_status=PostStatus.UNSUITABLE.value,
                details={"score": score, "reason": reason, "threshold": threshold}))
            await session.commit()
        log.info("пост %s: агрегатор отклонил (score %.1f < %d) — %s",
                 post_id, score, threshold,
                 (reason or "причина не сообщена, см. «Вызовы LLM»")[:120])
        return
    await _technical_approve(post_id, channel, from_status)


async def _technical_approve(post_id: int, channel, from_status) -> None:
    """Агрегатор: реклама отсечена префильтром — одобряем без классификации и карточек."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        if post.status not in (PostStatus.PREFILTERED, PostStatus.NEEDS_MEDIA_REVIEW):
            log.info("пост %s: техническое одобрение пропущено (статус %s)",
                     post_id, post.status.value)
            return
        if post.repost_pending:
            log.info("пост %s: уже в очереди пересылки — повторное одобрение пропущено", post_id)
            return
        done = (await session.execute(
            select(PublishJob.id).where(
                PublishJob.post_id == post_id,
                PublishJob.state == PublishJobState.DONE).limit(1))).scalar_one_or_none()
        if done is not None:
            log.warning("пост %s: уже опубликован — техническое одобрение пропущено", post_id)
            return
        prev = post.status.value
        post.status = PostStatus.APPROVED
        if channel.aggregate_mode == "repost":
            post.repost_pending = True
            post.repost_attempts = 0
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="technical_approved",
            from_status=prev, to_status=PostStatus.APPROVED.value,
            details={"channel": channel.username, "mode": channel.aggregate_mode}))
        if channel.aggregate_mode == "repost":
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="repost_queued",
                to_status=PostStatus.APPROVED.value,
                details={"channel": channel.username}))
        await session.commit()
    if channel.aggregate_mode == "credit":
        ok, msg = await create_publish_job(post_id, PublishMode.NOW)
        log.info("пост %s: технический канал -> публикация с подписью источника (%s)", post_id, msg)
    elif channel.aggregate_mode == "repost":
        # флаг и событие выставлены атомарно выше (вместе со сменой статуса)
        log.info("пост %s: поставлен в очередь пересылки в @%s", post_id, channel.username)
    else:  # none — только БД
        log.info("пост %s: технический канал -> сохранён в БД без публикации", post_id)


_SIG_RE = re.compile(r"подписат|подписывай|subscribe|наш канал", re.I)


def _sig_guard_hit(text: str) -> bool:
    """Подпись/ссылка t.me в последней строке черновика = стоп-автопилот."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return False
    last = lines[-1]
    return ("t.me/" in last) or ("telegram.me/" in last) or bool(_SIG_RE.search(last))


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
        async with session_scope() as session:
            post_g = await session.get(Post, post_id)
            ch_g = (await session.get(TargetChannel, post_g.target_channel_id)
                    if post_g is not None and post_g.target_channel_id is not None else None)
        draft_g = (post_g.draft_text or post_g.original_text or "") if post_g is not None else ""
        if ch_g is not None and ch_g.autopilot_sig_guard and _sig_guard_hit(draft_g):
            async with session_scope() as session:
                p = await session.get(Post, post_id)
                if p is not None:
                    p.double_check_note = ("автопилот остановлен предохранителем подписей: "
                                           "в последней строке ссылка t.me/призыв подписки — "
                                           "нужно ручное подтверждение")
                    session.add(PostEvent(
                        post_id=post_id, actor=EventActor.SYSTEM, action="autopilot_sig_guard",
                        from_status=PostStatus.AWAITING_REVIEW.value,
                        to_status=PostStatus.AWAITING_REVIEW.value,
                        details={"last_line": draft_g.strip().splitlines()[-1]
                                 if draft_g.strip() else ""}))
                    await session.commit()
            log.warning("пост %s: предохранитель подписей — автопилот остановлен, ждёт ручного подтверждения", post_id)
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


def _norm_line(s: str) -> str:
    """Нормализация для сравнения: NFKC, единые кавычки, схлопнутые пробелы, lower."""
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in (("«", '"'), ("»", '"'), ("“", '"'), ("”", '"'), ("’", "'")):
        s = s.replace(a, b)
    return " ".join(s.split()).strip().lower()


_URL_TOKEN_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/|@)[\w./\-]+")


def _urls_of(s: str) -> set:
    return set(_URL_TOKEN_RE.findall((s or "").lower()))


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _apply_clean_plan(lines: list, plan: list) -> tuple[list, list, str]:
    """Сверяет план модели с текстом и удаляет подписи КОДОМ.

    Первичный ключ — дословный текст подписи (устойчив к ошибкам нумерации и пустым строкам).
    Номер строки — ТОЛЬКО подсказка: точное совпадение текста всегда важнее номера.
    Статусы: ok | nothing | mismatch | ambiguous.
    """
    if not plan:
        return lines, [], "nothing"
    drop: set = set()
    for item in plan:
        raw_text = item.get("text") or ""
        want = _norm_line(raw_text)
        if not want:
            return lines, sorted(drop), "mismatch"   # без текста сверка невозможна
        want_urls = _urls_of(raw_text)
        cands = []
        for start in range(len(lines)):
            for span in (1, 2, 3):
                if start + span > len(lines):
                    break
                block = "\n".join(lines[start:start + span])
                window = _norm_line(block)
                if not window:
                    continue
                if want_urls and not (want_urls & _urls_of(block)):
                    continue
                score = _sim(want, window)
                if want in window or window in want:
                    shorter, longer = min(len(want), len(window)), max(len(want), len(window))
                    if longer and shorter / longer >= 0.6:
                        score = max(score, 0.99)
                if score >= 0.85:
                    cands.append((score, start, span))
        if not cands:
            return lines, sorted(drop), "mismatch"
        hint = item.get("i")
        exact = [c for c in cands if c[0] >= 0.995]
        if exact:
            # Точное совпадение по тексту: наименьшее окно; номер — только при равенстве
            min_span = min(c[2] for c in exact)
            picks = [c for c in exact if c[2] == min_span]
            if len(picks) > 1:
                picked = [c for c in picks if isinstance(hint, int) and c[1] == hint - 1]
                if len(picked) != 1:
                    return lines, sorted(drop), "ambiguous"
                best = picked[0]
            else:
                best = picks[0]
        else:
            cands.sort(key=lambda c: c[0], reverse=True)
            top = cands[0][0]
            ties = [c for c in cands if c[0] >= top - 0.02]
            if len(ties) > 1:
                picked = [c for c in ties if isinstance(hint, int) and c[1] == hint - 1]
                if len(picked) != 1:
                    return lines, sorted(drop), "ambiguous"
                best = min(picked, key=lambda c: c[2])
            else:
                best = ties[0]
        if isinstance(hint, int) and hint - 1 != best[1]:
            log.info("clean-plan: модель указала строку %d, по тексту найдена %d — доверяем тексту",
                     hint, best[1] + 1)
        for k in range(best[1], best[1] + best[2]):
            drop.add(k + 1)
    kept = [ln for i, ln in enumerate(lines, 1) if i not in drop]
    return kept, sorted(drop), "ok"


def _finalize_clean(kept: list, text: str):
    kept = [_BARE_URL_RE.sub("", ln).rstrip() for ln in kept]
    out = _strip_source_decor("\n".join(kept))
    return out if (out and out != text.strip()) else None


async def _clean_plan_call(post_id: int, listing: str, model: str, providers, label: str):
    """Один вызов модели очистки: план (номера строк + дословный текст)."""
    messages = [
        {"role": "system",
         "content": with_language_rules(CLEAN_SYSTEM, await _response_lang())},
        {"role": "user", "content": CLEAN_USER.format(listing=listing[:TEXT_LIMIT])},
    ]
    resp, result, call_status, error_text = await _call_and_parse(
        messages, model, 300, temperature=0.0,
        schema=CleanPlanResult, provider=providers,
        reasoning_max_tokens=settings.llm_reasoning_small,
    )
    if resp is not None and resp.cost_usd:
        await guards.add_llm_cost(resp.cost_usd)
    async with session_scope() as session:
        session.add(_make_call_row(post_id, LLMStage.REWRITE, model, CLEAN_VERSION, messages,
                                   resp, asdict(result) if result else None,
                                   call_status, error_text))
        await session.commit()
    if call_status is not LLMCallStatus.OK or result is None:
        log.warning("пост %s: очистка (%s, %s) не дала ответа: %s",
                    post_id, label, model, error_text)
        return None
    return result


async def _ensure_clean_draft(post_id: int) -> None:
    """Удаление декора источника: детерминированные шаблоны + LLM-обобщитель.

    Слой 1 (детерминированный) снимает известные формы: подписи, хэштеги, тизеры.
    Слой 2 (LLM) обобщает принцип «указатель vs контент» и ловит НОВЫЕ формулировки;
    его план проходит сверку «номер + дословный текст», поэтому безопасен.
    Итог = объединение множеств удаляемых строк; события clean_llm_extra позволяют
    дообучать шаблоны на пойманных моделью новых вариантах.
    Для чувствительных постов (post.sensitive) обе ступени очистки заменяются на
    llm_sensitive_model (если задана) — там, где базовые модели обрывают рассуждения.
    """
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        channel = await session.get(TargetChannel, post.target_channel_id) \
            if post.target_channel_id else None
        if channel is not None and channel.rewrite_enabled:
            return  # рерайт чистит декор сам (правило стиля)
        if channel is not None and channel.no_review and channel.aggregate_mode == "repost":
            return  # пересылка оригинала — текст не трогаем
        source = await session.get(Source, post.source_id) if post.source_id else None
        text = post.draft_text or post.original_text or ""
        sensitive = bool(getattr(post, "sensitive", False))
    source_username = source.username if source is not None else None

    lines = text.split("\n")
    det = set(_signature_lines(lines, source_username))

    # Слой 2 зовём только если после детерминистики остались ссылки/призывы-указатели
    kept_after_det = [ln for i, ln in enumerate(lines, 1) if i not in det]
    suspect = _DECOR_SUSPECT_RE.search("\n".join(kept_after_det)) is not None

    llm_drop: set = set()
    fallback_used = False
    attempts: list = []
    if suspect:
        listing = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, 1))
        cheap_model = await _model_for(Keys.PREFILTER_MODEL)
        cheap_providers = await _providers_for(Keys.PREFILTER_PROVIDERS)
        async with session_scope() as session:
            fb = str(await get_setting(session, Keys.CLEAN_FALLBACK_MODEL) or "").strip()
            sm = ""
            if sensitive:
                sm = str(await get_setting(session, Keys.LLM_SENSITIVE_MODEL) or "").strip()
        if sm:
            cheap_model = sm
            fb = sm
            log.info("пост %s: чувствительная лексика — очистка через %s", post_id, sm)
        strong_model = fb or await _model_for(Keys.DOUBLE_CHECK_MODEL)
        strong_providers = await _providers_for(Keys.DOUBLE_CHECK_PROVIDERS)
        for label, model, providers in (("clean", cheap_model, cheap_providers),
                                        ("clean-fallback", strong_model, strong_providers)):
            result = await _clean_plan_call(post_id, listing, model, providers, label)
            if result is None:
                continue
            _kept, dropped_l, verify = _apply_clean_plan(lines, result.remove)
            attempts.append({"model": model, "plan": result.remove[:5], "verify": verify})
            if verify == "ok":
                llm_drop = set(dropped_l)
                fallback_used = (label == "clean-fallback")
                break
            if verify == "nothing":
                break  # модель уверена, что декора нет

    drop = det | llm_drop
    extra = sorted(llm_drop - det)
    cleaned = None
    if drop:
        kept = [ln for i, ln in enumerate(lines, 1) if i not in drop]
        cleaned = _finalize_clean(kept, text)

    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            await session.commit()
            return
        if cleaned is not None:
            post.draft_text = cleaned
            post.draft_version += 1
            session.add(PostDraftVersion(post_id=post_id, version=post.draft_version,
                                         text=cleaned, origin=DraftOrigin.ORIGINAL))
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="clean_signatures",
                details={"mode": "det+llm" if llm_drop else "deterministic",
                         "lines": sorted(drop), "llm_extra": extra,
                         "fallback_used": fallback_used}))
            if extra:
                session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                                      action="clean_llm_extra", details={"lines": extra}))
            if fallback_used:
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="clean_fallback_used",
                    details={"first_model": attempts[0].get("model") if attempts else None,
                             "fallback_model": attempts[-1].get("model") if attempts else None,
                             "first_verify": attempts[0].get("verify") if attempts else None}))
            await session.commit()
            log.info("пост %s: очистка декора — шаблонами %s, моделью дополнительно %s",
                     post_id, sorted(det), extra or "—")
            return
        await session.commit()
        log.info("пост %s: декор не найден ни одним слоем — текст без изменений", post_id)


async def _run_double_check(post_id: int) -> tuple[bool, str]:
    """Двойная проверка черновика перед автопубликацией.

    Для постов с чувствительной лексикой (post.sensitive) используется отдельная
    модель llm_sensitive_model (если задана) — там, где базовые модели обрывают
    рассуждения из-за модерации провайдера.
    """
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return False, "пост не найден"
        channel = (await session.get(TargetChannel, post.target_channel_id)
                   if post.target_channel_id else None)
        source = await session.get(Source, post.source_id) if post.source_id else None
        draft = post.draft_text or post.original_text or ""
        verdict = post.verdict_reason or ""
        score = post.score
        relevance = source.relevance if source is not None else None
        title = channel.title if channel else "канал"
        desc = channel.description if channel else ""
        # Персональная инструкция владельца канала (что здесь считается допустимым)
        channel_note = channel.llm_instructions if channel is not None else None
        online = bool(channel.double_check_online) if channel is not None else False
        strictness = (channel.double_check_fact_strictness
                      if channel is not None and channel.double_check_fact_strictness
                      else settings.double_check_fact_strictness)

        base = (await get_setting(session, Keys.DOUBLE_CHECK_MODEL)) \
            or settings.effective_revision_model
        if online:
            chosen = (await get_setting(session, Keys.DOUBLE_CHECK_ONLINE_MODEL)) or base
            model = chosen + ":online"
            providers = await _providers_for(Keys.DOUBLE_CHECK_ONLINE_PROVIDERS)
        else:
            model = base
            providers = await _providers_for(Keys.DOUBLE_CHECK_PROVIDERS)

        # Чувствительный пост -> отдельная модель (если задана);
        # суффикс :online сохраняем, чтобы веб-инструмент остался включён
        if getattr(post, "sensitive", False):
            sm = str(await get_setting(session, Keys.LLM_SENSITIVE_MODEL) or "").strip()
            if sm:
                model = sm + (":online" if online else "")

    media_hint = await _media_hint(post_id)
    messages = [
        {"role": "system", "content": build_double_check_prompt(
            title, relevance, online, strictness, media_hint=media_hint,
            channel_note=channel_note,
            response_lang=await _response_lang())},
        {"role": "user", "content": DOUBLE_CHECK_USER.format(
            channel_description=desc,
            relevance=relevance if relevance is not None else "—",
            score=f"{score:.1f}" if score is not None else "—",
            verdict=verdict or "нет",
            draft=draft[:TEXT_LIMIT])},
    ]

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        resp, result, call_status, error_text = await _call_and_parse(
            messages, model, settings.llm_rewrite_max_tokens, temperature=0.1,
            schema=DoubleCheckResult, provider=providers,
            reasoning_max_tokens=(settings.llm_reasoning_online_check if online
                                  else settings.llm_reasoning_max_tokens),
        )
        if resp is not None and resp.cost_usd:
            await guards.add_llm_cost(resp.cost_usd)
        async with session_scope() as session:
            session.add(_make_call_row(
                post_id, LLMStage.REVISION, model, DOUBLE_CHECK_VERSION, messages,
                resp, asdict(result) if result else None, call_status, error_text))
            session.add(PostEvent(
                post_id=post_id, actor=EventActor.SYSTEM, action="double_check_attempt",
                details={"attempt": attempt,
                         "status": call_status.value if call_status else None,
                         "error": error_text},
            ))
            await session.commit()
        if result is not None:
            if attempt > 1:
                async with session_scope() as session:
                    session.add(PostEvent(
                        post_id=post_id, actor=EventActor.SYSTEM,
                        action="double_check_recovered",
                        details={"attempt": attempt},
                    ))
                    await session.commit()
                log.info("пост %s: двойная проверка ответила с попытки %d", post_id, attempt)
            return result.approve, result.note
        if attempt < max_attempts:
            log.warning("пост %s: двойная проверка не ответила (попытка %d) — повтор через 60 сек",
                        post_id, attempt)
            await asyncio.sleep(60)
    return False, "двойная проверка не дала ответа — нужна ручная проверка"


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