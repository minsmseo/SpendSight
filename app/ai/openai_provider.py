"""OpenAI implementation of LLMProvider (used when only OPENAI_API_KEY is present)."""
import base64
import json

from app.ai.provider import ProviderError, ToolTurn, parse_json_text


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str):
        import openai

        self._sdk = openai
        self.model = model
        self._client = openai.OpenAI(api_key=api_key, max_retries=1)

    def _create(self, *, timeout: float, **kwargs):
        sdk = self._sdk
        try:
            response = self._client.chat.completions.create(model=self.model, timeout=timeout, **kwargs)
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as exc:
            raise ProviderError("auth", str(exc))
        except sdk.RateLimitError as exc:
            quota = getattr(exc, "code", "") == "insufficient_quota" or "insufficient_quota" in str(exc)
            raise ProviderError("credit" if quota else "rate_limit", str(exc))
        except sdk.APIStatusError as exc:
            raise ProviderError("credit" if exc.status_code == 402 else "other", f"HTTP {exc.status_code}")
        except sdk.APITimeoutError:
            raise ProviderError("timeout", f"no response within {timeout:.0f}s")
        except sdk.APIConnectionError:
            raise ProviderError("network", "could not reach the OpenAI API")
        choice = response.choices[0]
        if choice.finish_reason == "content_filter" or getattr(choice.message, "refusal", None):
            raise ProviderError("refusal", "the model declined this request")
        if choice.finish_reason == "length":
            raise ProviderError("other", "response was cut off at the token limit")
        return choice.message

    def _json(self, system: str, content, schema: dict, timeout: float) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        strict = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
        try:
            message = self._create(timeout=timeout, messages=messages, response_format=strict)
        except ProviderError as exc:
            if exc.kind != "other":
                raise
            # Model without json_schema support: plain JSON mode with the schema in the prompt.
            messages[0]["content"] = f"{system}\nReply with one JSON object matching this JSON schema:\n{json.dumps(schema)}"
            message = self._create(timeout=timeout, messages=messages, response_format={"type": "json_object"})
        try:
            return parse_json_text(message.content)
        except ValueError:
            pass
        messages += [
            {"role": "assistant", "content": message.content or ""},
            {"role": "user", "content": "That was not valid JSON for the schema. Reply again with only the JSON object."},
        ]
        message = self._create(timeout=timeout, messages=messages, response_format={"type": "json_object"})
        try:
            return parse_json_text(message.content)
        except ValueError:
            raise ProviderError("bad_json", "model did not return valid JSON")

    def complete_json(self, system: str, user: str | list, schema: dict, timeout: float = 30) -> dict:
        return self._json(system, user, schema, timeout)

    def vision_json(self, image_bytes: bytes, mime: str, prompt: str, schema: dict, timeout: float = 45) -> dict:
        if not mime.startswith("image/"):
            raise ProviderError("other", "only images are sent to OpenAI vision; PDFs use local OCR")
        url = f"data:{mime};base64,{base64.standard_b64encode(image_bytes).decode('ascii')}"
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]
        return self._json("You read receipts precisely and never guess.", content, schema, timeout)

    def chat_with_tools(self, messages: list, tools: list[dict], timeout: float = 45, system: str = "") -> ToolTurn:
        functions = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools
        ]
        message = self._create(timeout=timeout, messages=[{"role": "system", "content": system}, *messages], tools=functions)
        calls = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            calls.append({"id": call.id, "name": call.function.name, "input": args})
        return ToolTurn(text=message.content or "", tool_calls=calls, raw_assistant=message.model_dump(exclude_none=True))

    def tool_result_messages(self, results: list[dict]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in results]
