"""The load test's fake LLM must satisfy the real SDK, prompts and schemas. If it did
not, every generation in a run would take the error path and the numbers would
describe the wrong code. Needs the `loadtest` dependency group; no database or network.
"""

import asyncio
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import unittest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "../src"))
sys.path.insert(0, SRC)
MOCK_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../loadtest/mock_services/app.py"))

os.environ.setdefault("JWT_SECRET_KEY", "x" * 40)
os.environ["MOCK_LLM_TTFT_MEDIAN"] = "0.01"
os.environ["MOCK_LLM_TOKENS_PER_S"] = "100000"
os.environ["MOCK_STT_SECONDS"] = "0"
os.environ["MOCK_TTS_SECONDS"] = "0"

import uvicorn  # noqa: E402

from backend.generation.content import CONTENT_MODELS, PARAM_MODELS, SUPPORTED_TYPES  # noqa: E402
from backend.generation.pipeline import _extract_json  # noqa: E402
from backend.generation.prompts import GEN_PROMPTS  # noqa: E402
from backend.llm.client import Message, StreamEnd, TokenDelta  # noqa: E402
from backend.llm.gemini import GeminiClient  # noqa: E402
from backend.llm.prompts import activity, ppt, quiz  # noqa: E402
from backend.ppt.schema import parse_deck  # noqa: E402


def _load_mock_app():
    spec = importlib.util.spec_from_file_location("mock_services_app", MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_MINIMAL_PARAMS = {"question_paper": {"short_count": 5}}


def _generation_prompt(gen_type: str) -> tuple[str, list[Message]]:
    _, build = GEN_PROMPTS[gen_type]
    return build(
        grade_label="Class 8",
        subject_name="Science",
        topic_title="Photosynthesis",
        topic_description=None,
        language="hi-BiharBoli",
        chunks=[],
        params=PARAM_MODELS[gen_type].model_validate(_MINIMAL_PARAMS.get(gen_type, {})),
    )


def _artifact_prompt(builder) -> tuple[str, list[Message]]:
    return builder.build(
        grade_label="Class 8",
        subject_name="Science",
        topic_title="Photosynthesis",
        topic_description=None,
        language="hi-BiharBoli",
        chunks=[],
        teacher_query="Explain it",
    )


class MockLLMContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.server = uvicorn.Server(
            uvicorn.Config(_load_mock_app(), host="127.0.0.1", port=cls.port, log_level="error")
        )
        threading.Thread(target=cls.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not cls.server.started:
            if time.time() > deadline:
                raise RuntimeError("mock server did not start")
            time.sleep(0.05)
        # Read by the real SDK when the client is built.
        os.environ["GOOGLE_GEMINI_BASE_URL"] = f"http://127.0.0.1:{cls.port}"
        os.environ["GOOGLE_API_KEY"] = ""

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def _client(self) -> GeminiClient:
        return GeminiClient(api_key="fake-key", model="gemini-flash-lite-latest")

    def _stream(self, system: str, messages: list[Message]) -> tuple[str, int, object]:
        async def run():
            parts, chunks, usage = [], 0, None
            async for event in self._client().stream(system=system, messages=messages):
                if isinstance(event, TokenDelta):
                    parts.append(event.text)
                    chunks += 1
                elif isinstance(event, StreamEnd):
                    usage = event.usage
            return "".join(parts), chunks, usage

        return asyncio.run(run())

    def test_each_generation_type_passes_its_real_schema(self):
        for gen_type in SUPPORTED_TYPES:
            with self.subTest(gen_type=gen_type):
                system, messages = _generation_prompt(gen_type)
                text, chunks, usage = self._stream(system, messages)
                self.assertGreater(chunks, 1, "must stream, not arrive whole")
                self.assertGreater(usage.output_tokens, 0)
                parsed = _extract_json(text)
                self.assertIsNotNone(parsed, f"{gen_type}: not JSON")
                CONTENT_MODELS[gen_type].model_validate(parsed)

    def test_each_generation_type_gets_its_own_shape(self):
        expected_key = {
            "lesson_plan": "periods_detail",
            "question_paper": "general_instructions",
            "notes": "important_terms",
            "presentation": "slides",
            "quiz": "questions",
        }
        for gen_type, key in expected_key.items():
            with self.subTest(gen_type=gen_type):
                system, messages = _generation_prompt(gen_type)
                text, _, _ = self._stream(system, messages)
                self.assertIn(key, _extract_json(text))

    def test_ask_medha_artifacts_parse(self):
        for builder, key in ((quiz, "questions"), (activity, "steps"), (ppt, "slides")):
            with self.subTest(builder=builder.__name__):
                text, _, _ = self._stream(*_artifact_prompt(builder))
                parsed = _extract_json(text)
                self.assertIn(key, parsed)
                if builder is ppt:
                    parse_deck(parsed)

    def test_plain_chat_gets_prose_not_json(self):
        text, chunks, _ = self._stream(
            "You are a helpful teaching assistant.",
            [Message(role="user", content="Explain photosynthesis")],
        )
        self.assertIsNone(_extract_json(text))
        self.assertGreater(len(text), 500)
        self.assertGreater(chunks, 1)

    def test_complete_call_returns_text_and_usage(self):
        result = asyncio.run(
            self._client().complete(
                system="Write a short title.",
                messages=[Message(role="user", content="Photosynthesis")],
                max_tokens=64,
            )
        )
        self.assertTrue(result.text)
        self.assertLess(len(result.text), 200)
        self.assertGreater(result.usage.output_tokens, 0)

    def test_voice_endpoints_match_the_real_client(self):
        import backend.speech.client as speech

        original_base, original_key = speech._BASE, speech.settings.sarvam_api_key
        speech._BASE = f"http://127.0.0.1:{self.port}"
        speech.settings.sarvam_api_key = "fake-key"
        try:
            transcript = asyncio.run(
                speech.transcribe(b"RIFF....WAVE" + b"\x00" * 50_000, filename="a.wav", content_type="audio/wav", language=None)
            )
            audio = asyncio.run(speech.synthesize("नमस्ते", language="hi-IN"))
        finally:
            speech._BASE, speech.settings.sarvam_api_key = original_base, original_key
        self.assertTrue(transcript.transcript)
        self.assertEqual(audio.audio_bytes[:4], b"RIFF")

    def test_429_rate_maps_to_the_backends_rate_limit_error(self):
        code = (
            "import importlib.util,os;"
            "from starlette.testclient import TestClient;"
            f"spec=importlib.util.spec_from_file_location('m',{json.dumps(MOCK_PATH)});"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "r=TestClient(m.app).post('/v1beta/models/x:streamGenerateContent',json={});"
            "print(r.status_code)"
        )
        import subprocess

        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "MOCK_LLM_429_RATE": "1"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.stdout.strip(), "429", result.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
