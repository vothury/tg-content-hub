"""Rich-сообщения Telegram (Bot API 10.1+): весь текст поста плюс медиа блоками
в ОДНОМ сообщении. Схема блоков: text-блок с entities плюс photo-блоки с InputFile.
Если метод недоступен или схема отклонена — вызывающий код падает в каскад
«альбом с заголовочной подписью + тело отдельным сообщением».
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Union

from aiogram import Bot
from aiogram.methods.base import TelegramMethod
from aiogram.types import FSInputFile, Message


class SendRichMessage(TelegramMethod[Message]):
    __api_method__ = "sendRichMessage"
    __returning__ = Message

    chat_id: Union[int, str]
    rich_message: Dict[str, Any]


async def send_rich(bot: Bot, chat_id: int, text: str, entities, media: list,
                    root: Path) -> int | None:
    blocks: list = [{"type": "text", "text": text,
                     "entities": [e.model_dump(by_alias=True, exclude_none=True)
                                  for e in (entities or [])]}]
    n = 0
    for m in media:
        if not m.get("downloaded") or not m.get("local_path"):
            continue
        mtype = getattr(m.get("media_type"), "value", m.get("media_type"))
        if mtype != "photo":
            continue
        p = root / m["local_path"]
        if not p.exists():
            continue
        blocks.append({"type": "photo", "media": FSInputFile(p)})
        n += 1
        if n >= 10:
            break
    if n == 0:
        return None
    msg = await bot(SendRichMessage(chat_id=chat_id, rich_message={"blocks": blocks}))
    return msg.message_id