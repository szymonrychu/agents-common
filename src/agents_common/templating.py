"""Sandboxed Jinja2 templating shared by the agents services (no I/O).

Templates run in an immutable sandbox (autoescape off, StrictUndefined, no loader, bounded
range, powers and repetition) inside a killable child process limited by time and memory.
Every failure raises `TemplateError`, which carries the template line when known.
"""

import json
import multiprocessing
import resource
import sys
from typing import Any

import jinja2.meta
from jinja2 import StrictUndefined
from jinja2 import TemplateError as Jinja2Error
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError

MAX_TEMPLATE_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
TRUNCATION_MARKER = "\n[truncated]"
MAX_RANGE = 10_000
MAX_EXPONENT = 1000
MAX_INT_BITS = 14_300  # about 4300 decimal digits
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_MEMORY_MB = 256


class TemplateError(Exception):
    """A template that does not validate or render; `line` is the template line when known."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


def _truncate(text: str) -> str:
    raw = text.encode()
    if len(raw) <= MAX_BODY_BYTES:
        return text
    return raw[:MAX_BODY_BYTES].decode(errors="ignore") + TRUNCATION_MARKER


def _bounded_range(*args: int) -> range:
    result = range(*args)
    if len(result) > MAX_RANGE:
        raise ValueError(f"range is limited to {MAX_RANGE} items")
    return result


def _from_json(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"from_json: invalid JSON: {exc}") from exc


class _BoundedEnvironment(ImmutableSandboxedEnvironment):
    """Refuses sequence repetition, powers and products large enough to exhaust CPU or memory."""

    # Also stops the compiler from constant-folding these operators before call_binop runs.
    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context, operator, left, right):
        if operator == "*":
            if isinstance(left, int) and not isinstance(right, int | float):
                left, right = right, left
            if isinstance(right, int) and hasattr(left, "__len__"):
                if len(left) * max(right, 0) > MAX_BODY_BYTES * 4:
                    raise SecurityError("repetition result too large")
            elif (
                isinstance(left, int)
                and isinstance(right, int)
                and left.bit_length() + right.bit_length() > MAX_INT_BITS
            ):
                raise SecurityError("product too large")
        elif operator == "**":
            if isinstance(right, int | float) and abs(right) > MAX_EXPONENT:
                raise SecurityError("exponent too large")
            if (
                isinstance(left, int)
                and isinstance(right, int)
                and left.bit_length() * right > MAX_INT_BITS
            ):
                raise SecurityError("power too large")
        return super().call_binop(context, operator, left, right)


_env = _BoundedEnvironment(autoescape=False, undefined=StrictUndefined)
_env.filters["from_json"] = _from_json
_env.globals["range"] = _bounded_range


def _check_size(template: str) -> None:
    if len(template.encode()) > MAX_TEMPLATE_BYTES:
        raise TemplateError(f"template is longer than the {MAX_TEMPLATE_BYTES} byte limit")


def _template_line(exc: BaseException) -> int | None:
    line = getattr(exc, "lineno", None)
    if line:
        return line
    tb = exc.__traceback__
    found = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename in ("<template>", "<unknown>"):
            found = tb.tb_lineno
        tb = tb.tb_next
    return found


def _describe(exc: BaseException) -> tuple[str, int | None]:
    line = _template_line(exc)
    where = f" (line {line})" if line else ""
    return f"{type(exc).__name__}{where}: {exc}", line


def validate(template: str) -> None:
    """Raise TemplateError when `template` is too large or has invalid syntax."""
    _check_size(template)
    try:
        _env.from_string(template)
    except Jinja2Error as exc:
        message, line = _describe(exc)
        raise TemplateError(f"invalid template: {message}", line) from exc


def undeclared_variables(template: str) -> list[str]:
    """Sorted names the template reads from its context."""
    _check_size(template)
    try:
        ast = _env.parse(template)
    except Jinja2Error as exc:
        message, line = _describe(exc)
        raise TemplateError(f"invalid template: {message}", line) from exc
    return sorted(jinja2.meta.find_undeclared_variables(ast))


def _render_in_process(template: str, context: dict[str, Any]) -> str:
    chunks: list[str] = []
    size = 0
    for chunk in _env.from_string(template).generate(**context):
        chunks.append(chunk)
        size += len(chunk.encode())
        if size > MAX_BODY_BYTES:
            break
    return _truncate("".join(chunks))


def _cap_memory(memory_mb: int) -> None:
    """Cap the child's address space at its current size plus `memory_mb` (Linux enforces it)."""
    baseline = 0
    try:
        with open("/proc/self/statm") as fh:
            baseline = int(fh.read().split()[0]) * resource.getpagesize()
    except (OSError, ValueError, IndexError):
        pass
    limit = baseline + memory_mb * 1024 * 1024
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (OSError, ValueError):
        if sys.platform == "linux":
            raise


def _child(conn, template, context, memory_mb) -> None:
    try:
        _cap_memory(memory_mb)
        conn.send((True, _render_in_process(template, context), None))
    except BaseException as exc:
        message, line = _describe(exc)
        conn.send((False, message, line))
    finally:
        conn.close()


def _mp_context():
    if "forkserver" in multiprocessing.get_all_start_methods():
        ctx = multiprocessing.get_context("forkserver")
        ctx.set_forkserver_preload(["jinja2", "agents_common.templating"])
        return ctx
    return multiprocessing.get_context("spawn")


def render(
    template: str,
    context: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    memory_mb: int = DEFAULT_MEMORY_MB,
) -> str:
    """Render `template` with `context` in a killable child bounded by time and memory."""
    _check_size(template)
    try:
        ctx = _mp_context()
        parent, child = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(child, template, context, memory_mb), daemon=True)
        proc.start()
    except Exception as exc:
        raise TemplateError(f"template render failed to start: {exc}") from exc
    child.close()
    try:
        if not parent.poll(timeout):
            raise TemplateError("template render timed out")
        try:
            ok, value, line = parent.recv()
        except EOFError as exc:
            raise TemplateError("template render failed: worker died") from exc
    finally:
        parent.close()
        if proc.is_alive():
            proc.kill()
        proc.join()
    if not ok:
        raise TemplateError(value, line)
    return value


def preview(
    template: str,
    context: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    memory_mb: int = DEFAULT_MEMORY_MB,
) -> dict[str, Any]:
    """Render for an editor: `{rendered, error{line,message}, variables}`; never raises."""
    try:
        variables = undeclared_variables(template)
    except TemplateError as exc:
        return {
            "rendered": None,
            "error": {"line": exc.line, "message": exc.message},
            "variables": [],
        }
    try:
        rendered = render(template, context, timeout=timeout, memory_mb=memory_mb)
    except TemplateError as exc:
        return {
            "rendered": None,
            "error": {"line": exc.line, "message": exc.message},
            "variables": variables,
        }
    return {"rendered": rendered, "error": None, "variables": variables}
