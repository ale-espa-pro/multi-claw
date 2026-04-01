# twilio/router.py
from fastapi import APIRouter, Form, Response, Request, HTTPException
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv
import os

load_dotenv()
router = APIRouter(prefix="/twilio", tags=["twilio"])

# Config desde .env
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Runner del agente
_agent_runner = None


def set_agent_runner(runner):
    global _agent_runner
    _agent_runner = runner


def validate_twilio_request(request: Request, form_data: dict) -> bool:
    """Valida firma de Twilio."""
    # TODO: Reactivar en producción
    return True


@router.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(...),
    From: str = Form(...),
    To: str = Form(None),
    MessageSid: str = Form(None),
):
    """Webhook para mensajes de WhatsApp."""
    form_data = {"Body": Body, "From": From, "To": To or "", "MessageSid": MessageSid or ""}
    if not validate_twilio_request(request, form_data):
        raise HTTPException(status_code=403, detail="Invalid signature")

    session_id = From.replace("whatsapp:", "").replace("+", "")
    user_message = Body.strip()
    twiml = MessagingResponse()

    if _agent_runner is None:
        twiml.message("Error: Agente no configurado.")
        return Response(content=str(twiml), media_type="application/xml")

    try:
        response = await _agent_runner.process_message(
            session_id=session_id,
            user_input=user_message,
        )
        print(f"Session: {session_id} \n message: {user_message} \n response: {response}")
        twiml.message(response)
    except Exception as e:
        print(f"Error: {e}")
        twiml.message("Error procesando tu mensaje.")

    return Response(content=str(twiml), media_type="application/xml")
