import os
from openai import OpenAI

MODEL = "anthropic/claude-sonnet-4-5"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class AnthropicClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            base_url=OPENROUTER_BASE_URL,
        )

    def chat(self, message: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": message})

        response = self.client.chat.completions.create(
            model=MODEL,
            messages=messages,
        )
        return response.choices[0].message.content

    def ping(self) -> bool:
        """Check connectivity with a minimal request."""
        result = self.chat("ping", system="Reply with one word: pong")
        return bool(result)
