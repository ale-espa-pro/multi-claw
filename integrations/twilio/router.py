# twilio/router.py
import asyncio
import time
from collections import defaultdict, deque

import redis.asyncio as redis
from fastapi import APIRouter, Response, Request, HTTPException
from twilio.rest import Client as TwilioRestClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv
import os

load_dotenv()
router = APIRouter(prefix="/twilio", tags=["twilio"])

# Config desde .env
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "true").lower() in {
    "1", "true", "yes", "y", "si", "sí"
}
TWILIO_ALLOWED_FROM = {
    value.strip()
    for value in os.getenv("TWILIO_ALLOWED_FROM", "").split(",")
    if value.strip()
}
TWILIO_RATE_LIMIT_PER_MINUTE = int(os.getenv("TWILIO_RATE_LIMIT_PER_MINUTE", "10"))
TWILIO_RATE_LIMIT_PER_DAY = int(os.getenv("TWILIO_RATE_LIMIT_PER_DAY", "100"))
TWILIO_GLOBAL_RATE_LIMIT_PER_MINUTE = int(os.getenv("TWILIO_GLOBAL_RATE_LIMIT_PER_MINUTE", "30"))
TWILIO_MAX_INBOUND_WORDS = int(os.getenv("TWILIO_MAX_INBOUND_WORDS", "1000"))
TWILIO_MAX_REPLY_WORDS = int(os.getenv("TWILIO_MAX_REPLY_WORDS", "250"))
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
REDIS_URL = os.getenv("REDIS_URL")

# Runner del agente
_agent_runner = None
_redis_client = None
_twilio_rest_client: TwilioRestClient | None = None
_background_replies: set[asyncio.Task] = set()
_local_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_local_seen_messages: dict[str, float] = {}


def set_agent_runner(runner):
    global _agent_runner
    _agent_runner = runner


def _get_twilio_rest_client() -> TwilioRestClient | None:
    global _twilio_rest_client
    if _twilio_rest_client is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        _twilio_rest_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_rest_client


async def _get_redis():
    global _redis_client
    if not REDIS_URL:
        return None
    if _redis_client is None:
        _redis_client = await redis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    return _redis_client


def _twilio_validation_url(request: Request) -> str:
    if not PUBLIC_BASE_URL:
        return str(request.url)
    query = request.url.query
    suffix = request.url.path
    if query:
        suffix += f"?{query}"
    return f"{PUBLIC_BASE_URL}{suffix}"


def validate_twilio_request(request: Request, form_data: dict) -> bool:
    """Valida firma de Twilio."""
    if not TWILIO_VALIDATE_SIGNATURE:
        return True
    if not TWILIO_AUTH_TOKEN:
        return False

    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        return False

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(_twilio_validation_url(request), form_data, signature)


def _is_allowed_sender(sender: str) -> bool:
    return not TWILIO_ALLOWED_FROM or sender in TWILIO_ALLOWED_FROM


def _split_words(text: str, max_words: int = TWILIO_MAX_REPLY_WORDS) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    chunks = []
    for start in range(0, len(words), max_words):
        chunks.append(" ".join(words[start:start + max_words]))
    return chunks


async def _redis_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    if limit <= 0:
        return True
    client = await _get_redis()
    if client is None:
        return _local_rate_limit(key, limit, window_seconds)

    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
        return int(count) <= limit
    except Exception:
        return _local_rate_limit(key, limit, window_seconds)


def _local_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    bucket = _local_rate_buckets[key]
    while bucket and now - bucket[0] >= window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


async def _check_rate_limits(sender: str) -> bool:
    safe_sender = sender.replace(":", "_").replace("+", "")
    minute_ok = await _redis_rate_limit(
        f"twilio:rate:{safe_sender}:minute",
        TWILIO_RATE_LIMIT_PER_MINUTE,
        60,
    )
    day_ok = await _redis_rate_limit(
        f"twilio:rate:{safe_sender}:day",
        TWILIO_RATE_LIMIT_PER_DAY,
        24 * 60 * 60,
    )
    global_ok = await _redis_rate_limit(
        "twilio:rate:global:minute",
        TWILIO_GLOBAL_RATE_LIMIT_PER_MINUTE,
        60,
    )
    return minute_ok and day_ok and global_ok


async def _send_whatsapp_reply(client: TwilioRestClient, to: str, from_: str, body: str):
    for chunk in _split_words(body):
        # El SDK de Twilio es síncrono; to_thread evita bloquear el event loop.
        await asyncio.to_thread(
            client.messages.create,
            to=to,
            from_=from_,
            body=chunk,
        )


async def _process_and_reply(session_id: str, user_message: str, sender: str, receiver: str):
    """Procesa el mensaje en background y responde vía la REST API de Twilio.

    Los webhooks de Twilio expiran a ~15s, así que el agente (que puede tardar
    minutos) no puede responder dentro del propio webhook.
    """
    try:
        response = await _agent_runner.process_message(
            session_id=session_id,
            user_input=user_message,
        )
        print(f"Session: {session_id} \n message: {user_message} \n response: {response}")
    except Exception as e:
        print(f"Error procesando mensaje de Twilio: {e}")
        response = "Error procesando tu mensaje."

    client = _get_twilio_rest_client()
    if client is None:
        return
    try:
        await _send_whatsapp_reply(client, to=sender, from_=receiver, body=response)
    except Exception as e:
        print(f"Error enviando respuesta por Twilio REST: {e}")


def _schedule_reply(session_id: str, user_message: str, sender: str, receiver: str):
    task = asyncio.create_task(_process_and_reply(session_id, user_message, sender, receiver))
    _background_replies.add(task)
    task.add_done_callback(_background_replies.discard)


async def _mark_message_seen(message_sid: str | None) -> bool:
    if not message_sid:
        return False

    client = await _get_redis()
    if client is None:
        now = time.time()
        expired = [sid for sid, expires_at in _local_seen_messages.items() if expires_at <= now]
        for sid in expired:
            _local_seen_messages.pop(sid, None)
        if message_sid in _local_seen_messages:
            return True
        _local_seen_messages[message_sid] = now + 24 * 60 * 60
        return False

    try:
        key = f"twilio:seen:{message_sid}"
        was_set = await client.set(key, "1", ex=24 * 60 * 60, nx=True)
        return not bool(was_set)
    except Exception:
        now = time.time()
        if message_sid in _local_seen_messages:
            return True
        _local_seen_messages[message_sid] = now + 24 * 60 * 60
        return False


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Webhook para mensajes de WhatsApp."""
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    if not validate_twilio_request(request, form_data):
        raise HTTPException(status_code=403, detail="Invalid signature")

    Body = form_data.get("Body", "")
    From = form_data.get("From", "")
    MessageSid = form_data.get("MessageSid")

    if not From:
        raise HTTPException(status_code=400, detail="Missing sender")

    if not _is_allowed_sender(From):
        raise HTTPException(status_code=403, detail="Sender not allowed")

    twiml = MessagingResponse()

    if MessageSid and await _mark_message_seen(MessageSid):
        return Response(content=str(twiml), media_type="application/xml")

    if not await _check_rate_limits(From):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    session_id = From.replace("whatsapp:", "").replace("+", "")
    user_message = Body.strip()

    if len(user_message.split()) > TWILIO_MAX_INBOUND_WORDS:
        twiml.message("Mensaje demasiado largo. Envíalo en partes más pequeñas.")
        return Response(content=str(twiml), media_type="application/xml")

    if _agent_runner is None:
        twiml.message("Error: Agente no configurado.")
        return Response(content=str(twiml), media_type="application/xml")

    receiver = form_data.get("To", "")
    if _get_twilio_rest_client() is not None and receiver:
        # ACK inmediato: el agente responde luego vía REST API en background.
        _schedule_reply(session_id, user_message, sender=From, receiver=receiver)
        return Response(content=str(twiml), media_type="application/xml")

    # Fallback síncrono (sin credenciales REST, p. ej. pruebas locales):
    # solo sirve para respuestas que lleguen antes del timeout del webhook.
    try:
        response = await _agent_runner.process_message(
            session_id=session_id,
            user_input=user_message,
        )
        print(f"Session: {session_id} \n message: {user_message} \n response: {response}")
        for chunk in _split_words(response):
            twiml.message(chunk)
    except Exception as e:
        print(f"Error: {e}")
        twiml.message("Error procesando tu mensaje.")

    return Response(content=str(twiml), media_type="application/xml")
