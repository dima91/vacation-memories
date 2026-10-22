import pytest
from fake_ollama import FakeOllama


@pytest.fixture
def fake_ollama():
    with FakeOllama() as server:
        yield server
