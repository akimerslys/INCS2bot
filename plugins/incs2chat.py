import asyncio
import datetime
import json
import logging
import random
import re
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.enums import ChatMembersFilter
from pyrogram.types import Chat, Message, MessageEntity, User
import requests
from tgentity import to_md

import config

import core

DISCORD_MESSAGE_LENGTH_LIMIT = 2000


async def is_administrator(chat: Chat, user: User) -> bool:
    admins = {admin.user.id async for admin in chat.get_members(filter=ChatMembersFilter.ADMINISTRATORS)}

    return user.id in admins


def correct_message_entities(entities: list[MessageEntity] | None,
                             original_text: str, new_text: str) -> list[MessageEntity] | None:
    """Correct message entities (a.k.a. Markdown formatting) for edited text."""

    if entities is None:
        return

    length_diff = len(original_text) - len(new_text)

    entities_i_to_remove = []
    for i, entity in enumerate(entities):
        entity.offset -= length_diff

        if entity.offset < 0:
            entities_i_to_remove.append(i)

    for i in reversed(entities_i_to_remove):
        entities.pop(i)

    return entities


def to_discord_markdown(message: Message) -> str:
    text = (to_md(message)
            .replace(r'\.', '.')
            .replace(r'\(', '(')  # god bless this feels awful
            .replace(r'\)', ')'))
    text = re.sub(r'~([^~]+)~', r'~~\1~~', text)

    return text


def wrap_text(text: str, max_length: int) -> list[str]:
    """
    Wraps the given text into multiple sections with a length <= ``max_length``.

    Prioritises wrapping by newlines, then spaces.
    """

    if len(text) <= max_length:
        return [text]

    text_parts = []
    while len(text) > max_length:
        longest_possible_part = text[:max_length]
        last_char_index = longest_possible_part.rfind('\n')
        if last_char_index == -1:
            last_char_index = longest_possible_part.rfind(' ')
        if last_char_index == -1:
            last_char_index = max_length

        new_part = text[:last_char_index]
        text_parts.append(new_part)
        if text[last_char_index].isspace():
            last_char_index += 1
        text = text[last_char_index:]

    text_parts.append(text)

    return text_parts


def process_discord_text(message: Message) -> list[str]:
    text = (to_discord_markdown(message) if message.entities
            else message.caption if message.caption
    else message.text if message.text
    else '')

    # fixme: can break formatting if wrapping happens in the middle of formatted section
    # fixme: (e.g. "**some [split] wise words**")
    # fixme severity: low
    return wrap_text(text, DISCORD_MESSAGE_LENGTH_LIMIT)


def translate_text(text: str, source_lang: str = 'RU', target_lang: str = 'EN') -> str | None:
    headers = {'Authorization': f'DeepL-Auth-Key {config.DEEPL_TOKEN}', 'Content-Type': 'application/json'}
    data = {'text': [text], 'source_lang': source_lang, 'target_lang': target_lang}

    r = requests.post('https://api-free.deepl.com/v2/translate', headers=headers, json=data)
    if r.status_code == 200:
        return r.json()['translations'][0]['text']

    logging.error('Failed to translate text.')
    logging.error(f'{text=}')
    logging.error(f'{r.status_code=}, {r.reason=}')


def post_to_discord_webhook(url: str, text: str, attachment: BytesIO = None):
    payload = {'content': text}

    if attachment:
        payload = {'payload_json': (None, json.dumps(payload)), 'files[0]': (attachment.name, attachment.getbuffer())}
        r = requests.post(url, files=payload)
    else:
        headers = {'Content-Type': 'application/json'}
        r = requests.post(url, json=payload, headers=headers)

    if r.status_code not in [200, 204]:  # Discord uses 204 as a success code (yikes)
        logging.error('Failed to post to Discord webhook.')
        logging.error(f'{payload=}')
        logging.error(f'{r.status_code=}, {r.reason=}, {r.text=}')


async def forward_to_discord(client: Client, message: Message):
    texts = process_discord_text(message)
    logging.info(texts)

    attachment: BytesIO | None
    try:
        # fixme: for every attachment this ^ function will be called multiple times
        # fixme: this could be fixed if we keep track of media groups but too lazy to implement it properly
        attachment = await client.download_media(message, in_memory=True)
    except ValueError:
        attachment = None

    for text in texts:
        if attachment:
            post_to_discord_webhook(config.DS_WEBHOOK_URL, text, attachment)
            post_to_discord_webhook(config.DS_WEBHOOK_URL_EN, translate_text(text, 'RU', 'EN'), attachment)
            attachment = None
        else:
            post_to_discord_webhook(config.DS_WEBHOOK_URL, text)
            post_to_discord_webhook(config.DS_WEBHOOK_URL_EN, translate_text(text, 'RU', 'EN'))


async def cs_l10n_update(message: Message):
    has_the_l10n_line = ((message.text and "Обновлены файлы локализации" in message.text)
                         or (message.caption and "Обновлены файлы локализации" in message.caption))

    if has_the_l10n_line:
        await message.reply_sticker('CAACAgIAAxkBAAID-l_9tlLJhZQSgqsMUAvLv0r8qhxSAAIKAwAC-p_xGJ-m4XRqvoOzHgQ')


async def filter_message(message: Message):
    senders = set()
    if message.via_bot:
        senders.add(message.via_bot.id)
    if message.forward_from:
        senders.add(message.forward_from.id)
    if message.forward_from_chat:
        senders.add(message.forward_from_chat.id)

    if senders & set(config.FILTERED_SENDERS):
        await message.delete()


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.command("ban"))
async def ban(client: Client, message: Message):
    chat = await client.get_chat(config.INCS2CHAT)

    admins = chat.get_members(filter=ChatMembersFilter.ADMINISTRATORS)
    admins = {admin.user.id async for admin in admins}

    if message.from_user.id not in admins:
        return await message.reply("Эта команда недоступна, Вы не являетесь разработчиком Valve.")

    if message.reply_to_message:
        original_msg = message.reply_to_message
        await chat.ban_member(original_msg.from_user.id)
        await message.reply(f"{original_msg.from_user.first_name} получил(а) VAC бан.")


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.command("unban"))
async def unban(client: Client, message: Message):
    chat = await client.get_chat(config.INCS2CHAT)

    if not await is_administrator(chat, message.from_user):
        return await message.reply("Эта команда недоступна, Вы не являетесь разработчиком Valve.")

    if message.reply_to_message:
        original_msg = message.reply_to_message
        await chat.unban_member(original_msg.from_user.id)
        await message.reply(f"VAC бан у {original_msg.from_user.first_name} был удалён.")


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.command("warn"))
async def warn(client: Client, message: Message):
    chat = await client.get_chat(config.INCS2CHAT)

    if not await is_administrator(chat, message.from_user):
        return await message.reply("Эта команда недоступна, Вы не являетесь разработчиком Valve.")

    if message.reply_to_message:
        og_msg = message.reply_to_message
        await og_msg.reply_animation('CgACAgQAAx0CTFFE8AABCAWQZhBlULJVdtDBZfHrfrDasDc3TgEAApsDAALuLsxTbHEvGmmxl6geBA')
    await message.delete()


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.command('echo'))
async def echo(client: Client, message: Message):
    chat = await client.get_chat(config.INCS2CHAT)

    await message.delete()
    if not await is_administrator(chat, message.from_user):
        return

    if message.reply_to_message:
        reply_to = message.reply_to_message
        should_reply = True
    else:
        reply_to = message
        should_reply = False

    if message.text:
        text = message.text.removeprefix('/echo').strip()

        if not text:
            msg = await message.reply('Пустой текст.', quote=False)
            await asyncio.sleep(5)
            await msg.delete()
            return

        entities = correct_message_entities(message.entities, message.text, text)
        return await reply_to.reply(text, entities=entities, quote=should_reply, disable_web_page_preview=True)

    caption = message.caption.removeprefix('/echo').strip()
    entities = correct_message_entities(message.entities, message.caption, caption)

    if message.animation:
        animation = message.animation.file_id
        return await reply_to.reply_animation(animation, quote=should_reply, caption=caption, caption_entities=entities)

    if message.audio:
        audio = message.audio.file_id
        return await reply_to.reply_audio(audio, quote=should_reply, caption=caption, caption_entities=entities)

    if message.document:
        document = message.document.file_id
        return await reply_to.reply_document(document, quote=should_reply, caption=caption, caption_entities=entities)

    if message.photo:
        photo = message.photo.file_id
        return await reply_to.reply_photo(photo, quote=should_reply, caption=caption, caption_entities=entities)

    if message.video:
        video = message.video.file_id
        return await reply_to.reply_video(video, quote=should_reply, caption=caption, caption_entities=entities)

    if message.voice:
        voice = message.voice.file_id
        return await reply_to.reply_voice(voice, quote=should_reply, caption=caption, caption_entities=entities)


@Client.on_message(filters.linked_channel & filters.chat(config.INCS2CHAT))
async def handle_new_post(client: Client, message: Message):
    is_sent_by_correct_chat = (message.sender_chat and message.sender_chat.id == config.INCS2CHANNEL)
    is_forwarded_from_correct_chat = (message.forward_from_chat and message.forward_from_chat.id == config.INCS2CHANNEL)

    if is_sent_by_correct_chat and is_forwarded_from_correct_chat:
        await cs_l10n_update(message)
        await forward_to_discord(client, message)


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.forwarded)
async def filter_forwards(_, message: Message):
    await filter_message(message)


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.via_bot)
async def filter_via_bot(_, message: Message):
    await filter_message(message)


@Client.on_message(filters.chat(config.INCS2CHAT) & filters.sticker)
async def meow_meow_meow_meow(client: Client, message: Message):
    chance = random.randint(0, 100)

    if message.sticker.file_unique_id == 'AgADtD0AAu4r4Ug' and chance < 5:
        await message.reply('мяу мяу мяу мяу')
        core.MEOW_MEOW_MEOW_IN_A_ROW += 1

        if core.MEOW_MEOW_MEOW_IN_A_ROW == 3:   # chance 0.05*0.05*0.05 = 0.000125 = 0.0125% = 1/10000
            syb_id = 802764912
            syb = await client.get_chat(syb_id)
            await client.ban_chat_member(config.INCS2CHAT,
                                         syb_id,
                                         datetime.datetime.now() + datetime.timedelta(days=30))  # на 30 дней нормис
            await message.reply(f'{syb.first_name} получил(а) VAC бан.')
