"""Gateway client for the authoring model.

Every call is written to disk (prompt, response, tokens, cost, duration) so
that what the model was asked and what it answered can be audited later.
"""

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from generator import config
from pipeline.store import atomic_write_json

RETRY_DELAYS_SEC = (2, 8, 30)


class TruncatedReply(RuntimeError):
    """The model's reply hit the output limit before it finished."""


@dataclass
class LLMResult:
    text: str
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    duration_sec: float
    log_path: Path | None
    served_by: str | None = None  # model name the LLM provider reports having used


_LOG_LOCK = threading.Lock()  # one process, many worker threads


class LLM:
    """Thin wrapper over the OpenAI-compatible gateway with retries and logging."""

    def __init__(self, model: str = config.MODEL):
        key, base = config.gateway_credentials()
        self.model = model
        self._client = OpenAI(base_url=base, api_key=key, timeout=600, max_retries=0)
        # Called as on_retry(purpose=..., attempt=..., rate_limited=..., error=..., wait_sec=..., log_dir=...)
        # each time a request is about to be retried. The batch pipeline uses
        # it to put retries in the ledger, where a too-high worker count shows.
        self.on_retry = None

    def chat(
        self,
        prompt: str,
        *,
        purpose: str,
        log_dir: Path | None = None,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.7,
        max_tokens: int = 32000,
        reasoning_tokens: int = 4000,
    ) -> LLMResult:
        """One chat completion, retried on transport errors and unusable output.

        ``reasoning_tokens`` caps the model's hidden reasoning. Without a cap
        GLM-5.1 can spend the whole ``max_tokens`` budget thinking and return
        nothing, after several minutes. The gateway ignores ``effort`` levels
        for this model but honours an explicit token cap.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs = {"extra_body": {"reasoning": {"max_tokens": reasoning_tokens}}}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt, delay in enumerate((0, *RETRY_DELAYS_SEC)):
            if attempt:
                rate_limited = _is_rate_limit(last_error)
                # A rate limit means "slow down": wait as long as the LLM provider
                # asks, or at least the longest backoff step.
                wait = max(delay, _retry_after(last_error) or (RETRY_DELAYS_SEC[-1] if rate_limited else 0))
                if self.on_retry is not None:
                    self.on_retry(
                        purpose=purpose,
                        attempt=attempt,
                        rate_limited=rate_limited,
                        error=f"{type(last_error).__name__}: {last_error}"[:300],
                        wait_sec=wait,
                        log_dir=log_dir,
                    )
                time.sleep(wait)
            started = time.time()
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            except Exception as exc:  # network, rate limit, gateway error
                last_error = exc
                continue

            text = response.choices[0].message.content or ""
            finish = response.choices[0].finish_reason
            usage = response.usage.model_dump() if response.usage else {}
            result = LLMResult(
                text=text,
                cost_usd=float(usage.get("cost") or 0.0),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                duration_sec=round(time.time() - started, 2),
                log_path=None,
                served_by=getattr(response, "model", None),
            )
            usable = bool(text.strip()) and finish != "length"
            # Unusable replies are logged too: they were paid for, and the
            # records must add up to what was actually spent.
            if log_dir is not None:
                result.log_path = self._log(log_dir, purpose, messages, result, attempt, usable, finish)
            if usable:
                return result
            if finish == "length":
                # Asking again for the same thing would be cut off again, and a
                # bigger budget only invites a bigger reply. Let the caller
                # re-ask and say that the output must be shorter.
                raise TruncatedReply(
                    f"the reply was cut off after {result.completion_tokens} tokens because it was too long; "
                    "write less: smaller files, less data, shorter tests"
                )
            last_error = RuntimeError(f"unusable response (finish_reason={finish})")

        raise RuntimeError(f"LLM call '{purpose}' failed after retries: {last_error}")

    def _log(self, log_dir: Path, purpose: str, messages, result: LLMResult, attempt: int, usable: bool, finish) -> Path:
        # Two calls for the same task can finish together (the solution and the
        # tests are written in parallel), and the number comes from counting
        # the files already there, so counting and writing happen under a lock.
        with _LOG_LOCK:
            return self._write_log(log_dir, purpose, messages, result, attempt, usable, finish)

    def _write_log(self, log_dir: Path, purpose: str, messages, result: LLMResult, attempt: int, usable: bool, finish) -> Path:
        log_dir.mkdir(parents=True, exist_ok=True)
        seq = len(list(log_dir.glob("*.json"))) + 1
        path = log_dir / f"{seq:02d}-{purpose}{'' if usable else '-discarded'}.json"
        atomic_write_json(
            path,
            {
                "purpose": purpose,
                "model": self.model,
                "served_by": result.served_by,
                "retries": attempt,
                "usable": usable,
                "finish_reason": finish,
                "duration_sec": result.duration_sec,
                "cost_usd": result.cost_usd,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "messages": messages,
                "response": result.text,
            },
        )
        return path


def _is_rate_limit(error: Exception | None) -> bool:
    return getattr(error, "status_code", None) == 429 or type(error).__name__ == "RateLimitError"


def _retry_after(error: Exception | None) -> float | None:
    """Seconds the LLM provider asked us to wait, if it said (capped at two minutes)."""
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        return min(float(headers.get("retry-after")), 120.0)
    except (TypeError, ValueError):
        return None


def parse_json(text: str):
    """Parse a JSON value from a model reply, tolerating a ```json fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])
