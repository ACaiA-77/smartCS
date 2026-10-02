"""Tokenizer-backed context budgets and shared history formatting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from context.models import ContextTokenizerError, ModelProfile


class TokenCounter:
    """Count prompt tokens using an injected tokenizer or tiktoken.

    Tiktoken counts are only provider-compatible estimates unless the caller
    supplies the provider's tokenizer explicitly and marks that fact. Unknown
    models (including Kimi/Moonshot) are never reported as native-precise.
    """

    def __init__(
        self,
        model_profile: ModelProfile | None = None,
        tokenizer: Any | None = None,
        *,
        tokenizer_name: str | None = None,
        native: bool = False,
    ) -> None:
        self.model_profile = model_profile or ModelProfile.from_env()
        self._tokenizer = tokenizer
        self._tokenizer_name = tokenizer_name
        self._native = bool(native)
        self._tiktoken_encoding = None
        self._diagnostics: dict[str, Any] = {}
        if self._tokenizer is None:
            self._init_tiktoken()
        elif not (callable(self._tokenizer) or callable(getattr(self._tokenizer, "encode", None))):
            raise TypeError("tokenizer must be callable or expose encode(text)")

    def _init_tiktoken(self) -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise ContextTokenizerError(
                "tiktoken is required for context budgeting; install requirements.txt or inject a tokenizer"
            ) from exc
        try:
            self._tiktoken_encoding = tiktoken.encoding_for_model(self.model_profile.name)
            encoding_name = getattr(self._tiktoken_encoding, "name", self.model_profile.name)
            model_mapping_found = True
        except KeyError:
            # Known BPE only; never substitute a character-count heuristic.
            self._tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
            encoding_name = "cl100k_base"
            model_mapping_found = False
        except Exception as exc:
            raise ContextTokenizerError("failed to initialize tiktoken encoding") from exc

        provider = (self.model_profile.provider or "unknown").lower()
        if provider in {"moonshot", "kimi"}:
            warning = (
                "Moonshot/Kimi native tokenizer and framing are not verified; "
                "tiktoken values are estimates and the safety margin remains necessary."
            )
        elif model_mapping_found:
            warning = (
                "tiktoken model encoding is provider-compatible, not a guarantee of exact "
                "server-side framing or model-version accounting."
            )
        else:
            warning = (
                f"No tiktoken mapping for {self.model_profile.name!r}; cl100k_base is an "
                "explicit estimate, not a native provider tokenizer."
            )
        self._diagnostics = {
            "tokenizer": f"tiktoken:{encoding_name}",
            "tokenizer_precision": "tiktoken_estimate_not_native",
            "tokenizer_warning": warning,
        }

    @property
    def diagnostics(self) -> dict[str, Any]:
        if self._tokenizer is not None:
            return {
                "tokenizer": self._tokenizer_name or type(self._tokenizer).__name__,
                "tokenizer_precision": "explicit_native" if self._native else "injected_not_certified_native",
                "tokenizer_warning": None if self._native else "Injected tokenizer was not marked as provider-native.",
            }
        return dict(self._diagnostics)

    def count_text(self, text: str) -> int:
        text = text or ""
        if self._tokenizer is not None:
            if callable(self._tokenizer):
                result = self._tokenizer(text)
                if isinstance(result, int):
                    return max(0, result)
                return len(result)
            return len(self._tokenizer.encode(text))
        if self._tiktoken_encoding is None:
            raise ContextTokenizerError("no tokenizer is configured")
        # User/document content is untrusted; marker-looking strings are plain
        # text and must not be rejected as tokenizer control tokens.
        return len(self._tiktoken_encoding.encode(text, disallowed_special=()))

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", message)
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    else:
                        # Include a stable marker so non-text content is not free.
                        parts.append("[non-text-content]")
                else:
                    parts.append(str(part))
            return "\n".join(parts)
        return str(content or "")

    def count_message(self, message: Any) -> int:
        # Reserve framing for the role, message boundary, and assistant reply prefix.
        return 5 + self.count_text(self._message_text(message))

    def count_messages(self, messages: list[Any]) -> int:
        # Each message has independent framing; content separators are counted by
        # encoding each final assembled message as one text value.
        return sum(self.count_message(message) for message in messages)


def profile_from_model(model: str | ModelProfile | None = None) -> ModelProfile:
    if isinstance(model, ModelProfile):
        return model
    return ModelProfile.from_env(model=model)


def history_context_text(
    history: list[dict[str, Any]],
    max_tokens: int | None = None,
    model: str | ModelProfile | None = None,
    *,
    tokenizer: Any | None = None,
) -> str:
    """Render the newest history that fits an explicit tokenizer-backed limit.

    ``max_tokens=None`` uses the model profile's available prompt budget.
    ``model`` can be a model name or a validated :class:`ModelProfile`.
    """

    profile = profile_from_model(model)
    limit = profile.prompt_budget if max_tokens is None else max_tokens
    if type(limit) is not int or limit <= 0:
        raise ValueError("max_tokens must be a positive integer or None")
    counter = TokenCounter(profile, tokenizer)
    parts: list[str] = []
    for message in reversed(history):
        role = str(message.get("role", "message"))
        content = str(message.get("content", "") or "")
        candidate = f"{role}: {content}"
        joined = "\n".join([candidate, *parts])
        # Include framing for the single history message that will hold this text.
        if 5 + counter.count_text(joined) > limit:
            break
        parts.insert(0, candidate)
    return "\n".join(parts)


def supported_max_output_kwargs(llm: Any) -> dict[str, int]:
    """Compatibility helper; invocation support is detected by ContextManager."""
    del llm
    return {}


__all__ = ["TokenCounter", "history_context_text", "profile_from_model", "supported_max_output_kwargs"]
