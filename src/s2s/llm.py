"""The single, auditable subprocess boundary for model-powered stages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter
from typing import TypeAlias, TypeVar

from .config import load_config
from .ledger import Ledger


CLAUDE_BASE_ARGS = ("claude", "-p", "--output-format", "json")
# These flags are load-bearing: omitting session suppression makes the scanner ingest
# transcripts from its own calls, and slash-command/skill resolution would let user
# skills leak into pipeline prompts. NOTE: `--bare` is deliberately absent — it skips
# OAuth/credential resolution in current CLIs (built for API-key CI), which breaks
# subscription-authenticated users. Context isolation comes instead from running the
# subprocess in a neutral empty working directory (see NEUTRAL_CWD below), which
# prevents project CLAUDE.md/skill pickup.
CLAUDE_REQUIRED_FLAGS = ("--no-session-persistence", "--disable-slash-commands")
CODEX_MODEL_PREFIX = "codex:"
# Discovered from Codex CLI 0.144.0.  ``--ask-for-approval`` is a global flag
# and therefore must precede ``exec``; the remaining flags belong to ``exec``.
# ``--ephemeral`` is load-bearing: ordinary Codex calls persist rollouts under
# ~/.codex/sessions, where the s2s scanner would otherwise ingest its own work.
CODEX_BASE_ARGS = (
    "codex",
    # Pipeline transport calls must not load the user's MCP servers: they are
    # irrelevant to structured one-shot calls, and a server that fails to
    # spawn or authenticate under sandboxed env is fatal to the whole run.
    "-c",
    "mcp_servers={}",
    "--ask-for-approval",
    "never",
    "exec",
    "--ephemeral",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--sandbox",
    "read-only",
    "--color",
    "never",
    "--json",
)
CORRECTIVE_SUFFIX = (
    "\n\nYour previous response was not valid for the required JSON schema. "
    "Return only a corrected JSON result that exactly satisfies the schema."
)
INPUT_DIGEST_LENGTH = 16

# Deliberately conservative, rough per-call estimates for an explicit bulk-work gate.
ESTIMATED_CALL_COST_USD = {"haiku": 0.003, "sonnet": 0.03, "opus": 0.15}
# Codex CLI JSONL does not expose stable billing data.  This deliberately rough
# flat guess exists only for the preflight confirmation gate, not accounting.
CODEX_FLAT_CALL_ESTIMATE_USD = 0.01

T = TypeVar("T")
R = TypeVar("R")


class LLMError(RuntimeError):
    """Base error raised by the local Claude CLI boundary."""


class ClaudeNotFoundError(LLMError):
    """Raised when the user's Claude CLI cannot be found on PATH."""


class CodexNotFoundError(LLMError):
    """Raised when an explicitly selected Codex CLI transport is unavailable."""


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
class LLMRequest:
    """Complete request handed to an injectable response transport.

    The environment snapshot is deliberate: eval providers can audit the exact
    HOME/S2S_HOME a real child process would inherit without spawning one.
    """

    argv: tuple[str, ...]
    prompt: str
    schema: Mapping[str, object]
    model: str
    stage: str
    timeout_s: float
    env: Mapping[str, str]

    @property
    def input_digest(self) -> str:
        return _input_digest(self.prompt)


ResponseEnvelope: TypeAlias = Mapping[str, object]
ResponseProvider: TypeAlias = Callable[[LLMRequest], ResponseEnvelope]

# Eval mode installs one provider for the duration of a synchronous pipeline run.
# Normal production calls leave this unset and take the subprocess path below.
response_provider: ResponseProvider | None = None


@contextmanager
def using_response_provider(provider: ResponseProvider | None):
    """Temporarily replace the transport at the one model-call chokepoint."""

    global response_provider
    previous = response_provider
    response_provider = provider
    try:
        yield
    finally:
        response_provider = previous


@dataclass(frozen=True)
class _Attempt:
    """One successful envelope parse, retained only long enough to return its result."""

    result: dict[str, object]


@lru_cache(maxsize=1)
def claude_supports_effort() -> bool:
    """Ask the installed CLI, rather than guessing, whether effort is supported."""

    try:
        completed = subprocess.run(
            ["claude", "-p", "--help"], capture_output=True, text=True, timeout=5, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "--effort" in (completed.stdout + completed.stderr)


def build_argv(
    schema: Mapping[str, object] | str, model: str, *, effort: str | None = None
) -> list[str]:
    """Construct the selected CLI command without shell interpolation."""

    if model.startswith(CODEX_MODEL_PREFIX):
        codex_model = model.removeprefix(CODEX_MODEL_PREFIX)
        if not codex_model:
            raise ValueError("codex: model strings must include a model name after the prefix")
        # Schema/output paths are per-attempt temporary files, so the provider
        # adds them immediately before spawning the process.
        return [*CODEX_BASE_ARGS, "--model", codex_model, "-"]

    schema_text, _ = _schema_text_and_data(schema)
    argv = [
        *CLAUDE_BASE_ARGS,
        "--json-schema",
        schema_text,
        "--model",
        model,
        *CLAUDE_REQUIRED_FLAGS,
    ]
    # Older CLIs have no equivalent flag. Keep profile data valid there, but do
    # not pretend a setting was applied when their help output says otherwise.
    if effort is not None and claude_supports_effort():
        argv.extend(("--effort", effort))
    return argv


def call(
    prompt: str,
    *,
    schema: Mapping[str, object] | str,
    model: str,
    stage: str,
    effort: str | None = None,
    timeout_s: float = 300,
) -> dict[str, object]:
    """Run one structured model request, retrying malformed output exactly once.

    Each subprocess attempt receives its own append-only ledger entry, including a
    failed first response before a corrective retry.
    """

    schema_text, schema_data = _schema_text_and_data(schema)
    argv = build_argv(schema_text, model, effort=effort)
    transport = _transport_for_model(model)

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
                    transport=transport,
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
    transport: str,
) -> _Attempt:
    tokens: int | None = None if transport == "codex" else 0
    cost_usd: float | None = None if transport == "codex" else 0.0
    started = perf_counter()
    deterministic_transport = bool(
        response_provider is not None
        and getattr(response_provider, "deterministic", False)
    )
    try:
        try:
            request = LLMRequest(
                argv=tuple(argv),
                prompt=prompt,
                schema=schema,
                model=model,
                stage=stage,
                timeout_s=timeout_s,
                env=dict(os.environ),
            )
            if response_provider is not None:
                envelope_value = response_provider(request)
            elif transport == "codex":
                envelope_value = codex_response_provider(request)
            else:
                envelope_value = default_response_provider(request)
        except FileNotFoundError as error:
            if transport == "codex":
                raise CodexNotFoundError(
                    "Codex CLI not found; install the OpenAI Codex CLI, run `codex login`, "
                    "and check PATH before using a codex:<model> setting."
                ) from error
            raise ClaudeNotFoundError(
                "Claude CLI not found; install claude and check PATH before running s2s."
            ) from error
        except subprocess.TimeoutExpired as error:
            cli_name = "Codex" if transport == "codex" else "Claude"
            raise LLMTimeout(f"{cli_name} CLI timed out after {timeout_s:g}s.") from error

        envelope = dict(envelope_value)
        if not isinstance(envelope, dict):
            raise MalformedOutputError("Claude CLI response must be a JSON object envelope.")
        if transport == "claude":
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
        # Cached/mock transports are deliberately timing-stable. Live subprocess
        # accounting retains real duration for cost and performance inspection.
        duration_ms = (
            0
            if deterministic_transport
            else round((perf_counter() - started) * 1000)
        )
        ledger.log_run(
            stage=stage,
            model=model,
            transport=transport,
            tokens=tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            input_digest=_input_digest(prompt),
        )


@lru_cache(maxsize=1)
def _neutral_cwd() -> str:
    """An empty directory for model subprocesses.

    Running from a neutral cwd prevents `claude -p` from loading whatever
    project CLAUDE.md/skills surround the caller's working directory —
    the context-isolation role `--bare` used to play before it proved to
    also skip subscription credentials.
    """

    # mkdtemp gives a per-process, 0o700, uniquely named directory. A fixed
    # predictable path in shared /tmp would let another local user pre-create
    # it and plant a CLAUDE.md that `claude -p` would ingest as project
    # context — prompt injection into every pipeline call.
    return tempfile.mkdtemp(prefix="s2s-neutral-cwd-")


def _real_user_home() -> str:
    """The invoking OS user's actual home, independent of $HOME overrides.

    Claude CLI authentication (OAuth/credentials under the user's real
    ~/.claude) must follow the OS user. Sandboxed eval runs override $HOME so
    the PIPELINE writes to fake targets — but the model transport still needs
    the real credentials, or every live call fails "Not logged in".
    """

    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


# Snapshot the auth-relevant environment at import time — before any eval
# sandbox mutates os.environ. CLAUDE_CONFIG_DIR relocates ALL claude storage
# including credentials, so a sandboxed value must never reach the transport;
# a user's own pre-existing value must always be honored.
_PROCESS_START_CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR")
_PROCESS_START_CODEX_HOME = os.environ.get("CODEX_HOME")


def default_response_provider(request: LLMRequest) -> dict[str, object]:
    """Route a live request for eval wrappers that install the default provider."""

    if _transport_for_model(request.model) == "codex":
        return codex_response_provider(request)
    return claude_response_provider(request)


def claude_response_provider(request: LLMRequest) -> dict[str, object]:
    """Execute the real Claude transport and return its parsed JSON envelope."""

    env = dict(request.env)
    env["HOME"] = _real_user_home()
    if _PROCESS_START_CLAUDE_CONFIG_DIR is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = _PROCESS_START_CLAUDE_CONFIG_DIR
    completed = subprocess.run(
        list(request.argv),
        input=request.prompt,
        capture_output=True,
        text=True,
        timeout=request.timeout_s,
        check=False,
        env=env,
        cwd=_neutral_cwd(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        # The CLI frequently reports failures as a JSON envelope on stdout
        # with a nonzero exit; losing stdout makes those undiagnosable.
        stdout_head = completed.stdout.strip()[:500]
        if _looks_like_auth_error(stderr) or _looks_like_auth_error(stdout_head):
            raise ClaudeAuthError(
                "Claude CLI is not authenticated; run `claude login` and try again."
            )
        detail = "".join(
            part
            for part in (
                f": {stderr}" if stderr else "",
                f" [stdout: {stdout_head}]" if stdout_head else "",
            )
        )
        flags = [a for a in request.argv if a.startswith("--") or a in ("claude", "-p")]
        env_fingerprint = {
            key: env.get(key, "<unset>")
            for key in ("HOME", "S2S_HOME", "CLAUDE_CONFIG_DIR", "PATH")
        }
        env_fingerprint["PATH"] = str(env_fingerprint["PATH"])[:120]
        raise ClaudeProcessError(
            f"Claude CLI exited with status {completed.returncode}{detail} "
            f"(flags={flags} model={request.model} env={env_fingerprint})"
        )
    parsed = _parse_json(completed.stdout, "Claude CLI response")
    if not isinstance(parsed, dict):
        raise MalformedOutputError("Claude CLI response must be a JSON object envelope.")
    return parsed


def _openai_strict_schema(schema: dict[str, object]) -> dict[str, object]:
    """Transform a schema to OpenAI structured-output strict form.

    Strict mode requires ``additionalProperties: false`` on every object and
    every property listed in ``required``. Anthropic's validation is looser,
    so schemas written for the claude transport fail codex verbatim
    (live finding: invalid_json_schema).
    """

    def walk(node: object) -> object:
        if isinstance(node, dict):
            out = {key: walk(value) for key, value in node.items()}
            if out.get("type") == "object":
                out.setdefault("properties", {})
                out["additionalProperties"] = False
                out["required"] = sorted(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)  # type: ignore[return-value]


def codex_response_provider(request: LLMRequest) -> dict[str, object]:
    """Execute an opt-in Codex transport and normalize its final JSON result.

    Codex's ``--json`` stream is useful diagnostics but is not a Claude-style
    response envelope.  ``--output-last-message`` gives us the exact assistant
    result, while ``--output-schema`` asks the CLI to constrain it before the
    existing local validator and one corrective retry run.
    """

    env = dict(request.env)
    env["HOME"] = _real_user_home()
    if _PROCESS_START_CODEX_HOME is None:
        env.pop("CODEX_HOME", None)
    else:
        env["CODEX_HOME"] = _PROCESS_START_CODEX_HOME

    with tempfile.TemporaryDirectory(prefix="s2s-codex-call-", dir=_neutral_cwd()) as directory:
        temporary_dir = Path(directory)
        schema_path = temporary_dir / "output.schema.json"
        output_path = temporary_dir / "last-message.json"
        schema_path.write_text(
            json.dumps(_openai_strict_schema(dict(request.schema)), separators=(",", ":")),
            encoding="utf-8",
        )
        argv = list(request.argv)
        stdin_marker = argv.pop() if argv and argv[-1] == "-" else None
        argv.extend(("--output-schema", str(schema_path), "--output-last-message", str(output_path)))
        if stdin_marker is not None:
            argv.append(stdin_marker)

        completed = subprocess.run(
            argv,
            input=request.prompt,
            capture_output=True,
            text=True,
            timeout=request.timeout_s,
            check=False,
            env=env,
            cwd=_neutral_cwd(),
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout_head = completed.stdout.strip()[:500]
            detail = "".join(
                part
                for part in (
                    f": {stderr}" if stderr else "",
                    f" [stdout: {stdout_head}]" if stdout_head else "",
                )
            )
            raise LLMError(f"Codex CLI exited with status {completed.returncode}{detail}")
        try:
            final_message = output_path.read_text(encoding="utf-8")
        except OSError as error:
            raise MalformedOutputError(
                "Codex CLI did not write its final response to --output-last-message."
            ) from error
        return {"result": _parse_json(final_message, "Codex result")}


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
    per_call = estimated_call_cost(model)
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

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
            raise SchemaValidationError(f"{path} must be at least {minimum}")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
            raise SchemaValidationError(f"{path} must be at most {maximum}")


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


def estimated_call_cost(model: str) -> float:
    """Return the documented preflight guess for one model call."""

    if model.lower().startswith(CODEX_MODEL_PREFIX):
        return CODEX_FLAT_CALL_ESTIMATE_USD
    normalized = model.lower()
    for family, cost in ESTIMATED_CALL_COST_USD.items():
        if family in normalized:
            return cost
    return ESTIMATED_CALL_COST_USD["sonnet"]


def _transport_for_model(model: str) -> str:
    return "codex" if model.startswith(CODEX_MODEL_PREFIX) else "claude"
