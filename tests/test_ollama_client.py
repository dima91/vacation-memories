import json
import socket

import pytest
from fake_ollama import VALID_ANSWER, Reply
from system_fakes import MODEL

from vacation_memories.ollama_client import OllamaClient, OllamaError
from vacation_memories.vision_schema import RESPONSE_SCHEMA


def client_for(server, **options) -> OllamaClient:
    return OllamaClient(server.url, MODEL, **options)


def chat(client: OllamaClient, image="aW1hZ2U=") -> str:
    return client.chat(prompt="Describe it.", image_b64=image, schema=RESPONSE_SCHEMA, num_predict=123)


def test_the_request_asks_for_structured_output_without_streaming_at_temperature_zero(fake_ollama):
    chat(client_for(fake_ollama, keep_alive="30m"))
    (request,) = fake_ollama.chat_requests
    assert request["model"] == MODEL
    assert request["stream"] is False
    assert request["format"] == RESPONSE_SCHEMA
    assert request["options"] == {"temperature": 0, "num_predict": 123}
    assert request["keep_alive"] == "30m"


def test_the_image_travels_in_base64_with_the_prompt_in_one_user_message(fake_ollama):
    chat(client_for(fake_ollama), image="aW1hZ2U=")
    (message,) = fake_ollama.chat_requests[0]["messages"]
    assert message == {"role": "user", "content": "Describe it.", "images": ["aW1hZ2U="]}


def test_the_content_of_the_answer_is_returned_as_text(fake_ollama):
    assert json.loads(chat(client_for(fake_ollama))) == VALID_ANSWER


def test_a_server_error_is_reported_with_its_status(fake_ollama):
    fake_ollama.script.append(Reply(status=500))
    with pytest.raises(OllamaError, match="500"):
        chat(client_for(fake_ollama))


def test_a_reply_that_takes_longer_than_the_timeout_is_an_error(fake_ollama):
    fake_ollama.script.append(Reply(VALID_ANSWER, delay=1.0))
    with pytest.raises(OllamaError, match="timed out"):
        chat(client_for(fake_ollama, timeout=0.2))


def test_an_unreachable_server_is_an_error():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(OllamaError):
        chat(OllamaClient(f"http://127.0.0.1:{port}", MODEL, timeout=1))


def test_an_answer_without_a_message_is_an_error(fake_ollama):
    client = client_for(fake_ollama)
    client._post = lambda path, body: {"done": True}
    with pytest.raises(OllamaError, match="message"):
        chat(client)


def test_unload_asks_ollama_to_free_the_model(fake_ollama):
    client = client_for(fake_ollama)
    chat(client)
    assert fake_ollama.loaded
    client.unload()
    assert fake_ollama.unload_requests == 1
    assert not fake_ollama.loaded


def test_unload_failure_is_an_error_the_caller_can_handle():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(OllamaError):
        OllamaClient(f"http://127.0.0.1:{port}", MODEL, timeout=1).unload()
