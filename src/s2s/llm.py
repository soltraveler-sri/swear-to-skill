"""The single, auditable subprocess boundary for Claude-powered stages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Any, TypeVar

from .config import load_config
from .ledger import Ledger


CLAUDE_BASE_ARGS = ("claude", "-p", "--output-format", "json")
# These flags are load-bearing: omitting session suppression makes the scanner ingest
# transcripts from its own calls, while --bare keeps user configuration out of runs.
CLAUDE_REQUIRED_FLAGS = ("--bare", "--no-session-persistence")
CORRECTIVE_SUFFIX = (
    "\n\nYour previous response was not valid for the required JSON schema. "
    "Return only a corrected JSON result that exactly satisfies the schema."
)
INPUT_DIGEST_LENGTH = 16

# Deliberately conservative, rough per-call estimates for an explicit bulk-work gate.
ESTIMATED_CALL_COST_USD = {"haiku": 0.003, "sonnet": 0.03, "opus": 0.15}

T = TypeVar("T")
R = TypeVar("R")


class LLMError(RuntimeError):
    """Base error raised by the local Claude CLI boundary."""


class ClaudeNotFoundError(LLMError):
    """Raised when the user's Claude CLI cannot be found on PATH."""


class ClaudeAuthError(LLMError):
    """Raised when Claude reports that its subscription session needs authentication."""


class LLMTimeout(LLMError):
    """Raised when Claude does not return before the requested timeout."""


class MalformedOutputError(LLMError):
    """Raised after Claude twice returns invalid JSON or a schema-invalid result."""


class ClaudeProcessError(LLMError):
    """Raised for a non-authentication nonzero Claude CLI exit."""


class SchemaValidationError(ValueError):
    """Internal error explaining which JSON-schema constraint was not met."""


@dataclass(frozen=True)
class _Attempt:
    """One successful envelope parse, retained only long enough to return its result."""

    result: dict[str, object]


def build_argv(schema: Mapping[str, object] | str, model: str) -> list[str]:
    """Construct the sole supported Claude CLI command without shell interpolation."""

    schema_text, _ = _schema_text_and_data(schema)
    return [
        *CLAUDE_BASE_ARGS,
        "--json-schema",
        schema_text,
        "--model",
        model,
        *CLAUDE_REQUIRED_FLAGS,
    ]


def call(
    prompt: str,
    *,
    schema: Mapping[str, object] | str,
    model: str,
    stage: str,
    timeout_s: float = 120,
) -> dict[str, object]:
    """Run one structured Claude request, retrying malformed output exactly once.

    Each subprocess attempt receives its own append-only ledger entry, including a
    failed first response before a corrective retry.
    """

    schema_text, schema_data = _schema_text_and_data(schema)
    argv = build_argv(schema_text, model)

    with Ledger() as ledger:
        for attempt_index in range(2):
            attempt_prompt = prompt if attempt_index == 0 else prompt + CORRECTIVE_SUFFIX
            try:
                return _call_once(
                    argv,
                    attempt_prompt,
                    schema_data,
                    ledger=ledger,
                    model=model,
                    stage=stage,
                    timeout_s=timeout_s,
                ).result
            except MalformedOutputError:
                if attempt_index == 1:
                    raise

    # The loop either returns or raises; this exists only to satisfy static readers.
    raise AssertionError("unreachable")


def _call_once(
    argv: list[str],
    prompt: str,
    schema: dict[str, object],
    *,
    ledger: Ledger,
    model: str,
    stage: str,
    timeout_s: float,
) -> _Attempt:
    tokens = 0
    cost_usd = 0.0
    started = perf_counter()
    try:
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as error:
            raise ClaudeNotFoundError(
                "Claude CLI not found; install claude and check PATH before running s2s."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise LLMTimeout(f"Claude CLI timed out after {timeout_s:g}s.") from error

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            if _looks_like_auth_error(stderr):
                raise ClaudeAuthError(
                    "Claude CLI is not authenticated; run `claude login` and try again."
                )
            detail = f": {stderr}" if stderr else ""
            raise ClaudeProcessError(
                f"Claude CLI exited with status {completed.returncode}{detail}"
            )

        envelope = _parse_json(completed.stdout, "Claude CLI response")
        if not isinstance(envelope, dict):
            raise MalformedOutputError("Claude CLI response must be a JSON object envelope.")
        usage = envelope.get("usage", {})
        tokens = _usage_tokens(usage)
        cost_usd = _cost(envelope.get("total_cost_usd"))
        if "result" not in envelope:
            raise MalformedOutputError("Claude CLI response did not include a result.")
        result = envelope["result"]
        if isinstance(result, str):
            result = _parse_json(result, "Claude result")
        if not isinstance(result, dict):
            raise MalformedOutputError("Claude result must be a JSON object.")
        try:
            validate_schema(result, schema)
        except SchemaValidationError as error:
            raise MalformedOutputError(f"Claude result does not satisfy the schema: {error}") from error
        return _Attempt(result=result)
    except json.JSONDecodeError as error:
        raise MalformedOutputError(f"Claude returned malformed JSON: {error.msg}") from error
    finally:
        duration_ms = round((perf_counter() - started) * 1000)
        ledger.log_run(
            stage=stage,
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            input_digest=_input_digest(prompt),
        )


def load_prompt(name: str, version: int | str) -> tuple[str, dict[str, object]]:
    """Load a versioned prompt and its sibling JSON-schema asset."""

    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name):
        raise ValueError("prompt name must contain only lowercase letters, digits, underscores, or hyphens")
    version_text = str(version)
    if version_text.startswith("v"):
        version_text = version_text[1:]
    if not version_text.isdigit() or int(version_text) < 1:
        raise ValueError("prompt version must be a positive integer")

    prompt_dir = Path(__file__).with_name("prompts")
    stem = f"{name}.v{version_text}"
    text = (prompt_dir / f"{stem}.md").read_text(encoding="utf-8")
    parsed = json.loads((prompt_dir / f"{stem}.schema.json").read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"prompt schema {stem!r} must contain a JSON object")
    return text, parsed


def run_batch(
    items: Iterable[T], worker: Callable[[T], R], parallelism: int | None = None
) -> list[R]:
    """Run independent subprocess-backed work with the configured bounded parallelism."""

    max_workers = load_config().models.parallelism if parallelism is None else parallelism
    if max_workers < 1:
        raise ValueError("parallelism must be at least 1")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(worker, items))


def estimate_and_confirm(n_calls: int, model: str, *, assume_yes: bool = False) -> bool:
    """Print a rough estimate and ask before a potentially material bulk spend."""

    if n_calls < 0:
        raise ValueError("n_calls cannot be negative")
    per_call = _estimated_call_cost(model)
    total = n_calls * per_call
    threshold = load_config().costs.confirm_threshold_usd
    print(
        f"Estimated LLM cost: {n_calls} {model} call(s) × ${per_call:.3f} = ${total:.2f}"
    )
    if total <= threshold or assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"Estimate exceeds ${threshold:.2f}; re-run with --yes to proceed.")
        return False
    return input("proceed? [y/N] ").strip().lower() in {"y", "yes"}


def validate_schema(value: object, schema: Mapping[str, object], path: str = "result") -> None:
    """Validate the small JSON-schema subset used by s2s prompt assets."""

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not all(isinstance(item, str) for item in expected_types) or not any(
            _matches_type(value, item) for item in expected_types
        ):
            expected = " or ".join(str(item) for item in expected_types)
            raise SchemaValidationError(f"{path} must be {expected}")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not any(_same_json_value(value, item) for item in enum):
            raise SchemaValidationError(f"{path} must be one of the schema enum values")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
            raise SchemaValidationError("schema required must be an array of strings")
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if properties is not None and not isinstance(properties, dict):
            raise SchemaValidationError("schema properties must be an object")
        for key, child_schema in (properties or {}).items():
            if key in value:
                if not isinstance(key, str) or not isinstance(child_schema, dict):
                    raise SchemaValidationError("schema properties must map strings to objects")
                validate_schema(value[key], child_schema, f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if not isinstance(item_schema, dict):
            raise SchemaValidationError("schema items must be an object")
        for index, item in enumerate(value):
            validate_schema(item, item_schema, f"{path}[{index}]")


def _schema_text_and_data(schema: Mapping[str, object] | str) -> tuple[str, dict[str, object]]:
    if isinstance(schema, str):
        parsed = json.loads(schema)
        if not isinstance(parsed, dict):
            raise ValueError("schema must be a JSON object")
        return schema, parsed
    parsed = dict(schema)
    return json.dumps(parsed, separators=(",", ":")), parsed


def _parse_json(value: str, description: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise MalformedOutputError(f"{description} was not valid JSON: {error.msg}") from error


def _usage_tokens(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key, value in usage.items():
        if "token" in str(key).lower() and isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
        elif isinstance(value, dict):
            total += _usage_tokens(value)
    return total


def _cost(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _input_digest(prompt: str) -> str:
    return sha256(prompt.encode("utf-8")).hexdigest()[:INPUT_DIGEST_LENGTH]


def _looks_like_auth_error(stderr: str) -> bool:
    text = stderr.lower()
    markers = (
        "not logged in",
        "not authenticated",
        "login required",
        "please login",
        "run claude login",
        "login",
        "authentication",
        "auth required",
        "unauthorized",
        "invalid credentials",
        "auth token",
        "sign in",
    )
    return any(marker in text for marker in markers)


def _matches_type(value: object, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _same_json_value(left: object, right: object) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _estimated_call_cost(model: str) -> float:
    normalized = model.lower()
    for family, cost in ESTIMATED_CALL_COST_USD.items():
        if family in normalized:
            return cost
    return ESTIMATED_CALL_COST_USD["sonnet"]
