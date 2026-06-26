import json
import re

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View
from .models import Lead, VoiceCall, VoiceMessage
from django.conf import settings
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from huggingface_hub import InferenceClient
import requests
from django.utils import timezone

# Hosted Hugging Face Inference Providers (not run locally -- no GPU/torch
# needed on this server, the request just goes out over HTTPS).
# provider="auto" routes to whichever backend is fastest for this model right
# now (Groq, Cerebras, etc. are all faster than HF's own shared GPU pool),
# which matters more for call latency than anything in our own code does.
client = InferenceClient(api_key=settings.HUGGINGFACE_TOKEN, provider="auto")

# How long Twilio waits for the customer to start speaking before giving up.
speaker_timeout = 5
# How long Twilio waits, after speech activity ends, before finalizing the
# transcript -- "auto" uses Twilio's own adaptive end-of-speech detection
# instead of a fixed multi-second silence wait, which is what made the
# original Gather feel slow to respond.
speech_timeout = "auto"
speech_language = "es-MX"  # recognition language for the customer's speech (Gather input)
# Plain <Say> with no voice defaults to Twilio's old robotic voice. Polly's
# neural voices sound like an actual person -- this is the output voice, a
# separate concern from speech_language above (which is about recognizing
# what the *customer* says, not how the agent sounds).
tts_voice = "Polly.Lupe-Generative"
tts_language = "es-US"

twilio_account_sid = settings.TWILIO_ACCOUNT_SID
twilio_auth_token = settings.TWILIO_AUTH_TOKEN

# Asking this small open model to embed a tagged JSON block inside its own
# conversational reply was unreliable -- it sometimes fired before the
# customer had actually confirmed, and once skipped the <order> tags
# entirely and nearly read raw JSON out loud. Order extraction is therefore
# a separate, single-purpose LLM call (see extract_order_json), triggered
# only by a deterministic check in code: the assistant's last turn must have
# asked for confirmation, AND the customer's reply must contain a real "yes".
CONFIRMATION_WORDS = (
    "si", "sí", "confirmo", "correcto", "correcta", "asi es", "así es",
    "exacto", "afirmativo", "dale", "vale", "esta bien", "está bien", "ok",
)
CONFIRMATION_PROMPT_WORDS = ("confirm",)


def customer_confirmed(speech: str) -> bool:
    normalized = speech.lower().strip()
    return any(word in normalized for word in CONFIRMATION_WORDS)


def last_assistant_asked_to_confirm(call) -> bool:
    last_assistant_message = (
        VoiceMessage.objects.filter(voice_chat=call, role="assistant").order_by("-timestamp").first()
    )
    if not last_assistant_message:
        return False
    normalized = last_assistant_message.content.lower()
    return any(word in normalized for word in CONFIRMATION_PROMPT_WORDS)

_menu_cache = {"text": "", "fetched_at": None}
MENU_CACHE_SECONDS = 300


def fetch_menu_text() -> str:
    """Fetch the live menu from RestoPOS via rpc/voice_get_menu, cached for a
    few minutes so every conversational turn doesn't refetch it."""
    now = timezone.now()
    cached_at = _menu_cache["fetched_at"]
    if cached_at and (now - cached_at).total_seconds() < MENU_CACHE_SECONDS:
        return _menu_cache["text"]

    if not (settings.SUPABASE_URL and settings.SUPABASE_ANON_KEY and settings.ORDER_WEBHOOK_API_KEY):
        return ""

    try:
        resp = requests.post(
            f"{settings.SUPABASE_URL}/rest/v1/rpc/voice_get_menu",
            json={"p_api_key": settings.ORDER_WEBHOOK_API_KEY},
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_ANON_KEY}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        menu = resp.json() or {}
    except Exception as e:
        print(f"Error fetching menu: {e}")
        return _menu_cache["text"]

    products = menu.get("products") or []
    if not products:
        return ""

    categories = {c["id"]: c["name"] for c in menu.get("categories") or []}
    lines_by_category = {}
    for product in products:
        category_name = categories.get(product.get("categoryId"), "Otros")
        line = f"- {product['name']}: {product['price']}"
        if product.get("description"):
            line += f" ({product['description']})"
        lines_by_category.setdefault(category_name, []).append(line)

    sections = [
        f"{name}:\n" + "\n".join(lines) for name, lines in lines_by_category.items()
    ]
    text = (
        "MENU ACTUAL (los unicos productos disponibles; usa exactamente estos "
        "nombres y precios, no inventes otros):\n\n" + "\n\n".join(sections)
    )
    _menu_cache["text"] = text
    _menu_cache["fetched_at"] = now
    return text


def build_system_prompt(extra_instructions: str = "") -> str:
    restaurant_name = settings.RESTAURANT_NAME or "el restaurante"
    menu_text = fetch_menu_text()
    return f"""Eres el asistente de voz de {restaurant_name}. Tomas pedidos de comida por telefono en espanol, de forma natural y amable.

Reglas:
- Solo ofrece productos que aparezcan textualmente en MENU ACTUAL. Si el cliente pide algo que no esta, dile que no esta disponible y sugiere 2-3 alternativas reales del menu.
- Confirma cada producto, cantidad y notas especiales que mencione el cliente.
- Pregunta si es para recoger (pickup) o entrega (delivery); si es entrega, pide la direccion completa.
- Pide el nombre del cliente y un numero de telefono de contacto.
- Antes de cerrar, repite el pedido completo y pregunta explicitamente "¿confirmas tu pedido?" o similar.
- Mantén tus respuestas a una o dos frases, como un agente de telefono eficiente.
- NUNCA leas el menu completo con precios y descripciones, ni siquiera si el cliente pregunta "que tienen" o "que hay disponible". En ese caso menciona solo 3 o 4 platos destacados (sin precios ni descripciones) y pregunta que se le antoja, o pide una categoria (bebidas, platos fuertes, postres) para ser mas especifico.
{extra_instructions}

{menu_text}
"""


def extract_order_json(call):
    """Single-purpose extraction call: given the conversation so far, return
    ONLY a JSON object with the confirmed order, or None.

    Kept separate from generate_reply() because asking this model to embed a
    tagged JSON block inside its own conversational answer was unreliable --
    it fired before the customer had actually said yes, and once skipped the
    <order> tags entirely. A dedicated extraction-only call, triggered by a
    deterministic confirmation check in code (not by the model's judgment),
    is far more reliable.
    """
    history_lines = [
        f"{'Cliente' if m.role != 'assistant' else 'Asistente'}: {m.content}"
        for m in VoiceMessage.objects.filter(voice_chat=call).order_by("timestamp")
    ]
    extraction_prompt = (
        "Basandote en esta conversacion entre el asistente de voz de un restaurante "
        "y un cliente, extrae el pedido final ya confirmado. Responde UNICAMENTE con "
        "un objeto JSON valido, sin texto adicional y sin markdown, con exactamente "
        "esta forma:\n"
        '{"items": [{"name": "...", "quantity": 1, "notes": ""}], "customer_name": "...", '
        '"customer_phone": "...", "delivery_type": "pickup o delivery", '
        '"delivery_address": "...", "special_instructions": "..."}\n\n'
        "Conversacion:\n" + "\n".join(history_lines)
    )
    try:
        res = client.chat.completions.create(
            model=settings.HF_MODEL_NAME,
            messages=[{"role": "user", "content": extraction_prompt}],
            max_tokens=300,
        )
        text = res.choices[0].message.content.strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        json.loads(text)  # validate before handing it back
        return text
    except Exception as e:
        print(f"Error extracting order JSON: {e}")
        return None


def submit_order(call_sid, customer_phone, order_json_text):
    """POST the confirmed order to the same RestoPOS webhook the Gemini-based
    voice agent uses (voice-order-webhook -> rpc/voice_create_order)."""
    try:
        order_data = json.loads(order_json_text)
    except (ValueError, TypeError) as e:
        print(f"Could not parse order JSON for call {call_sid}: {e}")
        return None

    payload = {
        "event": "order.created",
        "restaurant_id": settings.RESTAURANT_ID,
        "call_sid": call_sid,
        "customer": {
            "name": order_data.get("customer_name"),
            "phone": order_data.get("customer_phone") or customer_phone,
        },
        "order": {
            "items": order_data.get("items", []),
            "delivery_type": order_data.get("delivery_type"),
            "delivery_address": order_data.get("delivery_address"),
            "special_instructions": order_data.get("special_instructions"),
        },
        "created_at": timezone.now().isoformat(),
    }
    headers = {"Content-Type": "application/json"}
    if settings.ORDER_WEBHOOK_API_KEY:
        headers["Authorization"] = f"Bearer {settings.ORDER_WEBHOOK_API_KEY}"

    try:
        resp = requests.post(settings.ORDER_WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        body = resp.json() if resp.content else {}
        print(f"Order submission result for call {call_sid}: {body}")
        return body.get("order_id")
    except Exception as e:
        print(f"Error submitting order for call {call_sid}: {e}")
        return None


def generate_reply(call, max_tokens=180) -> str:
    """Run the LLM over the call's message history and return the speakable reply."""
    messages = [{"role": "system", "content": build_system_prompt()}]
    for m in VoiceMessage.objects.filter(voice_chat=call).order_by("timestamp"):
        role = "assistant" if m.role == "assistant" else "user"
        messages.append({"role": role, "content": m.content})

    try:
        res = client.chat.completions.create(
            model=settings.HF_MODEL_NAME, messages=messages, max_tokens=max_tokens
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error calling HF inference: {e}")
        return "Disculpa, tuve un problema tecnico. Puedes repetir, por favor?"


@method_decorator(csrf_exempt, name='dispatch')
class InboundCalls(View):
    def __init__(self):
        self.fallback_message = "No recibimos respuesta. Gracias por llamar. Hasta luego."
        self.closing_message = "Gracias por tu pedido. Que tengas un buen dia."

    def get(self, request):
        return render(request, "admin/aicaller/inbounds.html", {
            "data": request.GET,
            "callId": request.GET.get("CallSid"),
            "twilio_account_sid": twilio_account_sid,
            "twilio_auth_token": twilio_auth_token
        })

    def post(self, request):
        CallSid = request.POST.get("CallSid")
        customer_phone = request.POST.get("From")
        call = VoiceCall.objects.filter(call_id=CallSid).first()
        if not call:
            call = VoiceCall(
                call_id=CallSid, ai_caller="EchoLink", start_time=timezone.now(), call_type="inbound"
            )
            call.save()

        response = VoiceResponse()
        speech = request.POST.get('SpeechResult', "")

        if not speech:
            restaurant_name = settings.RESTAURANT_NAME or "nuestro restaurante"
            greeting = f"Hola, bienvenido a {restaurant_name}. Que le gustaria ordenar?"
            gather = Gather(
                input='speech', action=f'{settings.BASE_URL}/inbounds/',
                timeout=speaker_timeout, speech_timeout=speech_timeout, language=speech_language,
            )
            gather.say(greeting, voice=tts_voice, language=tts_language)
            response.append(gather)
            response.say(self.fallback_message, voice=tts_voice, language=tts_language)
            return HttpResponse(str(response), content_type='text/xml')

        should_extract_order = customer_confirmed(speech) and last_assistant_asked_to_confirm(call)

        VoiceMessage.objects.create(voice_chat=call, role="user", content=speech, call_id=CallSid)

        if should_extract_order:
            order_json = extract_order_json(call)
            if order_json:
                submit_order(CallSid, customer_phone, order_json)
                VoiceMessage.objects.create(
                    voice_chat=call, role="assistant", content=self.closing_message, call_id=CallSid
                )
                response.say(self.closing_message, voice=tts_voice, language=tts_language)
                response.hangup()
                return HttpResponse(str(response), content_type='text/xml')
            print(f"Order confirmation detected for call {CallSid} but extraction failed; continuing conversation.")

        speakable_reply = generate_reply(call)
        VoiceMessage.objects.create(voice_chat=call, role="assistant", content=speakable_reply, call_id=CallSid)

        gather = Gather(
            input='speech', action=f'{settings.BASE_URL}/inbounds/',
            timeout=speaker_timeout, speech_timeout=speech_timeout, language=speech_language,
        )
        gather.say(speakable_reply, voice=tts_voice, language=tts_language)
        response.append(gather)
        response.say(self.fallback_message, voice=tts_voice, language=tts_language)
        return HttpResponse(str(response), content_type='text/xml')


@method_decorator(csrf_exempt, name='dispatch')
class OutboundsCalls(View):
    def __init__(self):
        self.fallback_message = "No recibimos respuesta. Gracias por su tiempo. Hasta luego."
        self.closing_message = "Gracias por su pedido. Que tenga un buen dia."

    def get(self, request, id=None):
        call_sid = None
        lead = Lead.objects.get(pk=id)
        if id is not None:
            client_rest = Client(twilio_account_sid, twilio_auth_token)
            call = client_rest.calls.create(
                from_=settings.TWILIO_PHONE_NUMBER,
                to=lead.phone_number,
                url=f'{settings.BASE_URL}/outbounds/{id}',
                method="POST",
            )
            call_sid = call.sid
        return render(request, "admin/aicaller/outbounds.html", {
            "lead": lead,
            "callId": call_sid,
            "twilio_account_sid": twilio_account_sid,
            "twilio_auth_token": twilio_auth_token
        })

    def post(self, request, id=None):
        lead = Lead.objects.get(pk=id)
        CallSid = request.POST.get("CallSid")
        call = VoiceCall.objects.filter(call_id=CallSid).first()
        if not call:
            call = VoiceCall(
                lead=lead, call_id=CallSid, ai_caller="EchoLink",
                start_time=timezone.now(), call_type='outbound',
            )
            call.save()

        response = VoiceResponse()
        speech = request.POST.get('SpeechResult', "")

        if not speech:
            greeting = f"Hola, le habla {settings.RESTAURANT_NAME or 'el restaurante'}. Le gustaria hacer un pedido?"
            gather = Gather(
                input='speech', action=f'{settings.BASE_URL}/outbounds/{id}',
                timeout=speaker_timeout, speech_timeout=speech_timeout, language=speech_language,
            )
            gather.say(greeting, voice=tts_voice, language=tts_language)
            response.append(gather)
            response.say(self.fallback_message, voice=tts_voice, language=tts_language)
            return HttpResponse(str(response), content_type='text/xml')

        should_extract_order = customer_confirmed(speech) and last_assistant_asked_to_confirm(call)

        VoiceMessage.objects.create(voice_chat=call, role="user", content=speech, call_id=CallSid)

        if should_extract_order:
            order_json = extract_order_json(call)
            if order_json:
                submit_order(CallSid, lead.phone_number, order_json)
                VoiceMessage.objects.create(
                    voice_chat=call, role="assistant", content=self.closing_message, call_id=CallSid
                )
                response.say(self.closing_message, voice=tts_voice, language=tts_language)
                response.hangup()
                return HttpResponse(str(response), content_type='text/xml')
            print(f"Order confirmation detected for call {CallSid} but extraction failed; continuing conversation.")

        speakable_reply = generate_reply(call)
        VoiceMessage.objects.create(voice_chat=call, role="assistant", content=speakable_reply, call_id=CallSid)

        gather = Gather(
            input='speech', action=f'{settings.BASE_URL}/outbounds/{id}',
            timeout=speaker_timeout, speech_timeout=speech_timeout, language=speech_language,
        )
        gather.say(speakable_reply, voice=tts_voice, language=tts_language)
        response.append(gather)
        response.say(self.fallback_message, voice=tts_voice, language=tts_language)
        return HttpResponse(str(response), content_type='text/xml')
