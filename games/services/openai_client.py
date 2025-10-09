from __future__ import annotations
import json
from typing import Any, Dict, Optional
from django.conf import settings
from openai import OpenAI

class OpenAIClient:
    """
    Minimal wrapper around the OpenAI Responses API.
    - send_summary_json: sends your game summary as a JSON input item
    - send_summary_text: sends a rendered text prompt if you prefer plain text
    """

    def __init__(self: Optional[str] = None, default_model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=settings.OPEN_AI_TOKEN)
        self.default_model = default_model

    def send_summary_json(
        self,
        summary: Dict[str, Any],
        instructions: str = "Analyze the player's journey. Summarize insights, emotions, and behavioral patterns from answers. Highlight ladder/snake triggers and actionable recommendations.",
        model: Optional[str] = None,
        **response_kwargs: Any,
    ) -> str:
        """
        Send the collected summary as JSON using the Responses API.
        Returns the model's text output.
        """
        mdl = model or self.default_model

        resp = self.client.responses.create(
            model=mdl,
            instructions=instructions,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Here is a finished game summary in JSON."},
                        {"type": "input_json", "json": summary},
                    ],
                }
            ],
            **response_kwargs,
        )
        # Responses API returns "output" items; get the first text piece:
        for item in resp.output:
            if item.type == "message":
                for c in item.message.content:
                    if c.type == "output_text":
                        return c.text
        # Fallback: pretty-print the raw payload if no text:
        return json.dumps(resp.to_dict(), indent=2)

    def send_summary_text(
        self,
        prompt_text: str,
        model: Optional[str] = None,
        **response_kwargs: Any,
    ) -> str:
        mdl = model or self.default_model
        resp = self.client.responses.create(
            model=mdl,
            input=prompt_text,
            **response_kwargs,
        )
        for item in resp.output:
            if item.type == "message":
                for c in item.message.content:
                    if c.type == "output_text":
                        return c.text
        return ""
