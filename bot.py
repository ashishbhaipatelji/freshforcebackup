#    This file is part of the ForveSub distribution (https://github.com/xditya/ForceSub).
#    Copyright (c) 2021 Adiya
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, version 3.
#
#    This program is distributed in the hope that it will be useful, but
#    WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#    General Public License for more details.
#
#    License can be found in < https://github.com/xditya/ForceSub/blob/main/License> .

import logging
import asyncio
import os
from threading import Thread
from flask import Flask
from telethon.utils import get_display_name
import re
from telethon import TelegramClient, events, Button
from telethon.utils import get_peer_id
from decouple import config
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest

logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("BotzHub")
app = Flask(__name__)

@app.route("/")
def home():
    return "ForceSub Bot is running!", 200

def run_webserver():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_webserver, daemon=True).start()

# start the bot
log.info("Starting...")
try:
    bottoken = config("BOT_TOKEN")
    xchannel = config("CHANNEL")
    # Optional clickable join link. Set this for private channels/groups.
    channel_link = config("CHANNEL_LINK", default="")
    welcome_msg = config("WELCOME_MSG")
    welcome_not_joined = config("WELCOME_NOT_JOINED")
    on_join = config("ON_JOIN", cast=bool)
    on_new_msg = config("ON_NEW_MSG", cast=bool)
except Exception as e:
    log.error(e)
    log.info("Bot is quiting...")
    exit()

try:
    BotzHub = TelegramClient("BotzHub", 27194475, "b9eaaeead349eb9c593bfe9ae04ded7d").start(
        bot_token=bottoken
    )
except Exception as e:
    log.error(f"ERROR!\n{str(e)}")
    log.error("Bot is quiting...")
    exit()

# CHANNEL accepts a public @username, a t.me public URL, or a numeric chat ID
# (for a private channel/group, use its -100... ID and ensure the bot is an admin).
channel = xchannel.strip()
if channel.startswith("https://t.me/"):
    channel = channel.removeprefix("https://t.me/").strip("/")
elif channel.startswith("http://t.me/"):
    channel = channel.removeprefix("http://t.me/").strip("/")
channel = channel.lstrip("@")

# CHANNEL_LINK overrides the button URL; otherwise public usernames are used.
if channel_link:
    join_url = channel_link.strip()
    if not join_url.startswith(("https://t.me/", "http://t.me/")):
        raise ValueError("CHANNEL_LINK must be a Telegram t.me invite or public link")
elif channel.lstrip("-").isdigit():
    raise ValueError("For a numeric private CHANNEL ID, set CHANNEL_LINK to its t.me invite URL")
else:
    join_url = f"https://t.me/{channel}"

bot_self = BotzHub.loop.run_until_complete(BotzHub.get_me())
target_entity = None


# Auto-unmute timers: {(chat_id, user_id): asyncio.Task}
unmute_tasks = {}

# Auto-delete timers for messages sent by the bot.
message_delete_tasks = {}


async def delete_message_later(message, delay=120):
    """Delete a bot-generated message after exactly 2 minutes."""
    message_id = getattr(message, "id", None)
    chat_id = getattr(message, "chat_id", None)
    try:
        await asyncio.sleep(delay)

        if not message_id or not chat_id:
            log.error("AUTO-DELETE: missing message_id/chat_id")
            return

        # Use the client-level delete request explicitly. This is more reliable
        # for group/supergroup messages than relying on Message.delete().
        await BotzHub.delete_messages(chat_id, [message_id])
        log.info(
            "AUTO-DELETE SUCCESS: message %s deleted from chat %s after %s seconds",
            message_id, chat_id, delay
        )
    except asyncio.CancelledError:
        log.info("AUTO-DELETE CANCELLED: message %s", message_id)
    except Exception as e:
        log.error(
            "AUTO-DELETE FAILED: message %s in chat %s | %s: %s",
            message_id, chat_id, type(e).__name__, e
        )
    finally:
        if message_id is not None:
            message_delete_tasks.pop(message_id, None)


def schedule_message_delete(message, delay=120):
    """Schedule a bot message for automatic deletion."""
    task = asyncio.create_task(delete_message_later(message, delay))
    message_delete_tasks[message.id] = task
    log.info(
        "AUTO-DELETE SCHEDULED: message %s in chat %s will be deleted in %s seconds",
        message.id, getattr(message, "chat_id", "?"), delay
    )
    return task


async def send_temporary_message(event, message, buttons=None, delay=120):
    """Send a message; auto-delete only in groups, never in the bot's PM."""
    sent = await event.reply(message, buttons=buttons)
    # Keep bot replies in private chats (including /start) permanently.
    if event.is_private:
        return sent
    schedule_message_delete(sent, delay)
    return sent


def cancel_unmute_task(chat_id, user_id):
    """Cancel an existing auto-unmute timer for a user."""
    task = unmute_tasks.pop((chat_id, user_id), None)
    if task and not task.done():
        task.cancel()


async def auto_unmute(chat_id, user_id):
    """Unmute a user automatically after 2 minutes."""
    try:
        await asyncio.sleep(120)

        # If the user has joined the required channel, keep them unmuted.
        # If they have not joined, this is still the requested temporary
        # 2-minute unmute; their next message will mute them again.
        await BotzHub.edit_permissions(
            chat_id,
            user_id,
            until_date=None,
            send_messages=True,
        )

        log.info("Auto-unmuted user %s in chat %s after 2 minutes", user_id, chat_id)

    except asyncio.CancelledError:
        # Timer was cancelled because the user joined or a new timer replaced it.
        pass
    except Exception as e:
        log.error("Auto-unmute error for user %s in chat %s: %s", user_id, chat_id, e)
    finally:
        current = unmute_tasks.get((chat_id, user_id))
        if current is asyncio.current_task():
            unmute_tasks.pop((chat_id, user_id), None)


def start_unmute_timer(chat_id, user_id):
    """Start/restart the 2-minute auto-unmute timer."""
    cancel_unmute_task(chat_id, user_id)
    task = asyncio.create_task(auto_unmute(chat_id, user_id))
    unmute_tasks[(chat_id, user_id)] = task



# Resolve the required target once. For private targets, CHANNEL should be the
# numeric -100... chat ID (the bot must be a member/admin); CHANNEL_LINK is the
# invite URL shown to users. Public targets can use @username or t.me/username.
async def resolve_target():
    global target_entity
    try:
        target_entity = await BotzHub.get_entity(int(channel) if channel.lstrip("-").isdigit() else channel)
        log.info("Force-sub target resolved: %s", getattr(target_entity, "title", channel))
    except Exception as e:
        log.error("Cannot resolve CHANNEL=%r. Use @username for public targets or the numeric -100... ID for private targets. Error: %s", channel, e)
        raise


# Telegram does not allow bot accounts to list pending join requests via
# GetChatInviteImportersRequest. Track join-request updates delivered to this bot.
# This is in-memory: requests already pending before startup are not discoverable.
pending_join_requests = {}  # user_id -> timestamp


@BotzHub.on(events.Raw)
async def track_join_request(update):
    if update.__class__.__name__ != "UpdateBotChatInviteRequester":
        return
    try:
        peer_id = get_peer_id(update.peer)
        target_id = get_peer_id(target_entity)
        if peer_id != target_id:
            return
        user_id = int(update.user_id)
        # Telegram sends this update when a user requests to join.
        pending_join_requests[user_id] = True  # Only presence matters; date may be datetime.datetime.
        log.info("Tracked pending join request for user %s", user_id)
    except Exception as e:
        log.warning("Could not process join-request update: %s", e)


# Membership check: members pass; a pending request received while this process
# is running is temporarily treated as subscribed.
async def get_user_join(user_id):
    try:
        await BotzHub(GetParticipantRequest(channel=target_entity, participant=user_id))
        pending_join_requests.pop(int(user_id), None)
        return True
    except UserNotParticipantError:
        return int(user_id) in pending_join_requests
    except Exception as e:
        log.warning("Membership check failed for user %s: %s", user_id, e)
        return int(user_id) in pending_join_requests


@BotzHub.on(events.ChatAction)
async def _(event):
    if on_join is False:
        return
    if not event.is_group:
        return
    if event.action_message:
        return
    if event.user_joined or event.user_added:
        user = await event.get_user()
        chat = await event.get_chat()
        title = chat.title or "this chat"
        pp = await BotzHub.get_participants(chat)
        count = len(pp)
        mention = f"[{get_display_name(user)}](tg://user?id={user.id})"
        name = user.first_name
        last = user.last_name
        fullname = f"{name} {last}" if last else name
        username = f"@{uu}" if (uu := user.username) else mention
        x = await get_user_join(user.id)
        pending_join_requests.pop(int(user.id), None)
        if x is True:
            msg = welcome_msg.format(
                mention=mention,
                title=title,
                fullname=fullname,
                username=username,
                name=name,
                last=last,
                channel=join_url,
                count=count,
            )
            butt = [Button.url("Channel", url=join_url)]
        else:
            msg = welcome_not_joined.format(
                mention=mention,
                title=title,
                fullname=fullname,
                username=username,
                name=name,
                last=last,
                channel=join_url,
                count=count,
            )
            butt = [
                Button.url("Channel", url=join_url),
                Button.inline("UnMute Me", data=f"unmute_{user.id}"),
            ]
            await BotzHub.edit_permissions(
                event.chat.id, user.id, until_date=None, send_messages=False
            )
            start_unmute_timer(event.chat.id, user.id)

        await send_temporary_message(event, msg, buttons=butt)


@BotzHub.on(events.NewMessage(incoming=True))
async def mute_on_msg(event):
    if event.is_private:
        return
    if on_new_msg is False:
        return
    if not event.sender_id:
        return

    try:
        x = await get_user_join(event.sender_id)
        temp = await BotzHub.get_entity(event.sender_id)

        if x is False:
            if temp.bot:
                return

            # User has sent a message while not subscribed:
            # mute them again and start a fresh 2-minute timer.
            try:
                await BotzHub.edit_permissions(
                    event.chat.id,
                    event.sender_id,
                    until_date=None,
                    send_messages=False,
                )
                start_unmute_timer(event.chat.id, event.sender_id)
            except Exception as e:
                log.error("Mute/timer error: %s", e)
                return

            user = await event.get_sender()
            chat = await event.get_chat()
            title = chat.title or "this chat"
            pp = await BotzHub.get_participants(chat)
            count = len(pp)
            mention = f"[{get_display_name(user)}](tg://user?id={user.id})"
            name = user.first_name
            last = user.last_name
            fullname = f"{name} {last}" if last else name
            username = f"@{uu}" if (uu := user.username) else mention

            reply_msg = welcome_not_joined.format(
                mention=mention,
                title=title,
                fullname=fullname,
                username=username,
                name=name,
                last=last,
                channel=join_url,
                count=count,
            )

            butt = [
                Button.url("Channel", url=join_url),
                Button.inline(
                    "UnMute Me",
                    data=f"unmute_{event.sender_id}",
                ),
            ]
            await send_temporary_message(event, reply_msg, buttons=butt)

        else:
            # If the user is already subscribed, make sure any old timer
            # is stopped and leave them unmuted.
            cancel_unmute_task(event.chat.id, event.sender_id)

    except Exception as e:
        log.error("Message handler error: %s", e)


@BotzHub.on(events.callbackquery.CallbackQuery(data=re.compile(b"unmute_(.*)")))
async def _(event):
    uid = int(event.data_match.group(1).decode("UTF-8"))
    if uid == event.sender_id:
        x = await get_user_join(uid)
        nm = event.sender.first_name
        if x is False:
            await event.answer(
                f"You haven't joined the required channel/group yet!", cache_time=0, alert=True
            )
        elif x is True:
            try:
                cancel_unmute_task(event.chat.id, uid)
                await BotzHub.edit_permissions(
                    event.chat.id, uid, until_date=None, send_messages=True
                )
            except Exception as e:
                log.error(e)
                return
            msg = f"Welcome to {(await event.get_chat()).title}, {nm}!\nGood to see you here!"
            butt = [Button.url("Channel", url=join_url)]
            edited_msg = await event.edit(msg, buttons=butt)
            schedule_message_delete(edited_msg, 120)
    else:
        await event.answer(
            "You are an old member and can speak freely! This isn't for you!",
            cache_time=0,
            alert=True,
        )


@BotzHub.on(events.NewMessage(pattern="^/start$"))
async def strt(event):
    await send_temporary_message(
        event,
        f"Hi. I'm a force subscribe bot for the configured channel/group!\n\nCheckout @BotzHub :)",
        buttons=[
            Button.url("Channel", url=join_url),
            Button.url("Repository", url="https://github.com/xditya/ForceSub"),
        ],
    )


try:
    BotzHub.loop.run_until_complete(resolve_target())
except Exception:
    log.error("ForceSub cannot start until CHANNEL is configured correctly.")
    raise

log.info("ForceSub Bot has started as @%s.\nDo visit @BotzHub!", bot_self.username)
BotzHub.run_until_disconnected()
