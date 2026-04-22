"""OpenAI-compatible LLM client with configurable base URL for Ollama or any OpenAI-API provider."""

from dataclasses import dataclass, field
from typing import Iterator

from openai import OpenAI


@dataclass
class ClientConfig:
    model: str = "gpt-oss:120b"
    base_url: str = "http://localhost:11434/v1"  # Ollama default
    api_key: str = "ollama"                       # Ollama ignores this; real APIs need a real key
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_params: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )

    def complete(self, messages: list[dict]) -> str:
        response = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            **self.config.extra_params,
        )
        return response.choices[0].message.content

    def stream(self, messages: list[dict]) -> Iterator[str]:
        stream = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
            **self.config.extra_params,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
