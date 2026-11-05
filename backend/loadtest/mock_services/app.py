"""Fake Gemini and Sarvam, so a load test spends no money and hits no provider quota.

The backend keeps using its real clients and the real Gemini SDK, pointed here:

    GOOGLE_GEMINI_BASE_URL=http://<host>:9100
    SARVAM_BASE_URL=http://<host>:9100

Responses are valid for what the backend expects, because `pipeline.py` rejects
output that does not match its content schema, and a run full of rejected output
would exercise the error path instead of the real one.

Delays are tunable through the environment. The defaults are placeholders, so
replace them with figures from production `llm_call` logs (`latency_ms`,
`output_tokens`) when you have them:

    MOCK_LLM_TTFT_MEDIAN   median seconds to the first token
    MOCK_LLM_TTFT_SIGMA    spread of a log-normal around that median
    MOCK_LLM_TOKENS_PER_S  output speed
    MOCK_STT_SECONDS / MOCK_TTS_SECONDS   voice API delay
    MOCK_LLM_429_RATE      fraction of calls answered 429, to test quota errors

    uvicorn app:app --host :: --port 9100 --app-dir loadtest/mock_services
"""

import asyncio
import base64
import io
import json
import os
import random
import wave

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

TTFT_MEDIAN = float(os.environ.get("MOCK_LLM_TTFT_MEDIAN", "0.8"))
TTFT_SIGMA = float(os.environ.get("MOCK_LLM_TTFT_SIGMA", "0.5"))
TOKENS_PER_S = float(os.environ.get("MOCK_LLM_TOKENS_PER_S", "120"))
STT_SECONDS = float(os.environ.get("MOCK_STT_SECONDS", "0.8"))
TTS_SECONDS = float(os.environ.get("MOCK_TTS_SECONDS", "0.7"))
RATE_LIMIT_FRACTION = float(os.environ.get("MOCK_LLM_429_RATE", "0"))

CHARS_PER_TOKEN = 4
CHUNK_TOKENS = 12

CALLS = Counter("mock_llm_calls", "Fake LLM calls.", ("kind", "shape"))
ACTIVE = Gauge("mock_llm_active_streams", "Fake LLM streams in progress.")
VOICE = Counter("mock_voice_calls", "Fake voice API calls.", ("kind",))

_FILLER = (
    "प्रकाश संश्लेषण वह प्रक्रिया है जिसमें हरे पौधे सूर्य के प्रकाश की सहायता से अपना भोजन बनाते हैं। "
    "कक्षा में इसे समझाने के लिए एक पत्ती और पानी के गिलास का उपयोग करें। "
)


def _prose(chars: int) -> str:
    return (_FILLER * (chars // len(_FILLER) + 1))[:chars]


def _quiz() -> dict:
    return {
        "questions": [
            {
                "q": _prose(120),
                "type": "mcq",
                "options": [_prose(40) for _ in range(4)],
                "answer": _prose(40),
                "difficulty": "medium",
                "explanation": _prose(160),
            }
            for _ in range(10)
        ]
    }


def _notes() -> dict:
    return {
        "sections": [
            {"heading": _prose(50), "body_md": _prose(900), "key_points": [_prose(90)] * 4}
            for _ in range(5)
        ],
        "summary": _prose(500),
        "important_terms": [{"term": _prose(25), "meaning": _prose(120)} for _ in range(8)],
    }


def _lesson_plan() -> dict:
    return {
        "topic": _prose(60),
        "periods": 3,
        "periods_detail": [
            {
                "period_no": n,
                "concept": _prose(250),
                "learning_objective": _prose(250),
                "learning_outcomes": _prose(250),
                "teacher_learning_process": _prose(700),
                "assessment": _prose(250),
                "resources": _prose(120),
            }
            for n in (1, 2, 3)
        ],
        "homework": _prose(200),
    }


def _question_paper() -> dict:
    return {
        "total_marks": 50,
        "duration_min": 120,
        "general_instructions": [_prose(100)] * 4,
        "sections": [
            {
                "name": f"Section {name}",
                "instructions": _prose(100),
                "questions": [
                    {"text": _prose(200), "marks": 2, "type": "short", "options": []}
                    for _ in range(8)
                ],
            }
            for name in "ABC"
        ],
    }


def _deck() -> dict:
    return {
        "title": _prose(60),
        "subtitle": _prose(60),
        "slides": [
            {
                "layout": "bullets",
                "heading": _prose(50),
                "bullets": [_prose(90) for _ in range(5)],
                "notes": _prose(250),
            }
            for _ in range(10)
        ],
    }


def _activity() -> dict:
    return {
        "title": _prose(50),
        "materials": [_prose(30)] * 3,
        "group_size": 4,
        "duration_min": 20,
        "steps": [_prose(150) for _ in range(6)],
        "variation": _prose(150),
    }


# Checked in order, most specific first. Each marker is a key that only that
# type's prompt asks the model to return.
_JSON_SHAPES = (
    ("lesson_plan", "periods_detail", _lesson_plan),
    ("question_paper", "general_instructions", _question_paper),
    ("notes", "important_terms", _notes),
    ("deck", '"slides"', _deck),
    ("activity", '"steps"', _activity),
    ("quiz", '"questions"', _quiz),
)
_CACHED_JSON = {name: json.dumps(build(), ensure_ascii=False) for name, _, build in _JSON_SHAPES}


def _choose_response(system_text: str, max_tokens: int | None) -> tuple[str, str]:
    """Return (shape, text). Prose is for chat, voice and titles, which set no JSON
    shape. Short limits are one-liners such as titles and voice replies."""
    for name, marker, _ in _JSON_SHAPES:
        if marker in system_text:
            return name, _CACHED_JSON[name]
    if max_tokens is not None and max_tokens <= 512:
        return "short", _prose(60)
    return "prose", _prose(1800)


def _chunk_payload(text: str, *, last: bool, prompt_tokens: int, output_tokens: int) -> dict:
    payload: dict = {
        "candidates": [
            {
                "index": 0,
                "content": {"role": "model", "parts": [{"text": text}]},
                **({"finishReason": "STOP"} if last else {}),
            }
        ]
    }
    if last:
        payload["usageMetadata"] = {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "totalTokenCount": prompt_tokens + output_tokens,
        }
    return payload


def _request_details(body: dict) -> tuple[str, int | None, int]:
    system = body.get("systemInstruction") or body.get("system_instruction") or {}
    system_text = " ".join(p.get("text", "") for p in system.get("parts", []))
    config = body.get("generationConfig") or {}
    prompt_chars = len(system_text) + sum(
        len(p.get("text", "")) for c in body.get("contents", []) for p in c.get("parts", [])
    )
    return system_text, config.get("maxOutputTokens"), prompt_chars // CHARS_PER_TOKEN


def _rate_limited() -> JSONResponse | None:
    if RATE_LIMIT_FRACTION and random.random() < RATE_LIMIT_FRACTION:
        return JSONResponse(
            {"error": {"code": 429, "message": "fake quota", "status": "RESOURCE_EXHAUSTED"}},
            status_code=429,
        )
    return None


def _ttft() -> float:
    return random.lognormvariate(0, TTFT_SIGMA) * TTFT_MEDIAN


async def _gemini(request: Request) -> Response:
    """POST /{version}/models/{model}:{action}. Starlette's router cannot split the
    `model:action` form, so the action is parsed here."""
    action = request.path_params["model_action"].rsplit(":", 1)[-1]
    if action not in ("generateContent", "streamGenerateContent"):
        return JSONResponse({"error": {"code": 404, "message": action}}, status_code=404)

    limited = _rate_limited()
    if limited is not None:
        return limited

    system_text, max_tokens, prompt_tokens = _request_details(await request.json())
    shape, text = _choose_response(system_text, max_tokens)
    output_tokens = len(text) // CHARS_PER_TOKEN

    if action == "generateContent":
        CALLS.labels("complete", shape).inc()
        await asyncio.sleep(_ttft() + output_tokens / TOKENS_PER_S)
        return JSONResponse(
            _chunk_payload(text, last=True, prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        )

    CALLS.labels("stream", shape).inc()

    async def stream():
        ACTIVE.inc()
        try:
            await asyncio.sleep(_ttft())
            chunk_chars = CHUNK_TOKENS * CHARS_PER_TOKEN
            delay = CHUNK_TOKENS / TOKENS_PER_S
            pieces = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
            for index, piece in enumerate(pieces):
                last = index == len(pieces) - 1
                payload = _chunk_payload(
                    piece, last=last, prompt_tokens=prompt_tokens, output_tokens=output_tokens
                )
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n"
                if not last:
                    await asyncio.sleep(delay)
        finally:
            ACTIVE.dec()

    return StreamingResponse(stream(), media_type="text/event-stream")


def _build_wav_base64(seconds: float = 3.0) -> str:
    """Silence at the rate Sarvam returns, so the backend handles realistic bytes."""
    rate = 22050
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


_WAV_BASE64 = _build_wav_base64()


async def _speech_to_text(request: Request) -> JSONResponse:
    VOICE.labels("stt").inc()
    await request.body()
    await asyncio.sleep(STT_SECONDS)
    return JSONResponse(
        {"transcript": "प्रकाश संश्लेषण को सरल तरीके से समझाइए", "language_code": "hi-IN"}
    )


async def _text_to_speech(request: Request) -> JSONResponse:
    VOICE.labels("tts").inc()
    await request.body()
    await asyncio.sleep(TTS_SECONDS)
    return JSONResponse({"audios": [_WAV_BASE64]})


async def _metrics(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/health", _health),
        Route("/metrics", _metrics),
        Route("/speech-to-text", _speech_to_text, methods=["POST"]),
        Route("/text-to-speech", _text_to_speech, methods=["POST"]),
        Route("/{version}/models/{model_action}", _gemini, methods=["POST"]),
    ]
)
