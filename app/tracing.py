"""Langfuse tracing behind a tiny interface.

Rules (from the design):
- Tracing must never break the app: every call here swallows its own errors, and the SDK sends
  data from a background thread, so a Langfuse outage costs nothing but the traces.
- Personal data is masked before it leaves the process, user IDs are hashed, and by default
  document and question text is not sent at all (`tracing.log_content: false`): traces carry
  IDs, scores, counts and timings.
"""
import hashlib
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator

log = logging.getLogger("tracing")


def hash_user(username: str | None) -> str | None:
    return hashlib.sha256(username.encode()).hexdigest()[:12] if username else None


class NullSpan:
    trace_id: str | None = None

    def child(self, name: str, as_type: str = "span", **kw: Any) -> "NullSpan":
        return self

    def update(self, **kw: Any) -> None: ...

    def end(self) -> None: ...

    def score(self, name: str, value: float, comment: str | None = None) -> None: ...


class NullTracer:
    enabled = False
    log_content = False

    def start(self, name: str, **kw: Any) -> NullSpan:
        return NullSpan()

    def score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None: ...

    def text(self, s: str) -> str:
        return f"[{len(s)} chars]"

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class _Span:
    def __init__(self, raw):
        self._raw = raw
        self.trace_id = getattr(raw, "trace_id", None)

    def child(self, name: str, as_type: str = "span", **kw: Any) -> "_Span | NullSpan":
        try:
            return _Span(self._raw.start_observation(name=name, as_type=as_type, **kw))
        except Exception:
            log.warning("tracing: child span failed", exc_info=True)
            return NullSpan()

    def update(self, **kw: Any) -> None:
        try:
            self._raw.update(**kw)
        except Exception:
            log.warning("tracing: update failed", exc_info=True)

    def end(self) -> None:
        try:
            self._raw.end()
        except Exception:
            log.warning("tracing: end failed", exc_info=True)

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        try:
            self._raw.score_trace(name=name, value=float(value), comment=comment)
        except Exception:
            log.warning("tracing: score failed", exc_info=True)


class LangfuseTracer:
    enabled = True

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        mask_fn: Callable[[str], str] | None = None,
        log_content: bool = False,
        environment: str = "dev",
        release: str | None = None,
        **client_kw: Any,
    ):
        from langfuse import Langfuse

        self._mask_fn = mask_fn
        self.log_content = log_content
        self.environment = environment
        self._lf = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=host,
            environment=environment,
            release=release,
            mask=self._mask if mask_fn else None,
            **client_kw,
        )

    def _mask(self, *, data: Any, **_: Any) -> Any:
        """Applied by the SDK to every input, output and metadata before sending."""
        try:
            return self._walk(data)
        except Exception:
            return "[masking failed: withheld]"

    def _walk(self, d: Any) -> Any:
        if isinstance(d, str):
            return self._mask_fn(d) if d else d
        if isinstance(d, dict):
            return {k: self._walk(v) for k, v in d.items()}
        if isinstance(d, (list, tuple)):
            return [self._walk(v) for v in d]
        return d

    def text(self, s: str) -> str:
        return s if self.log_content else f"[{len(s)} chars]"

    def start(self, name: str, user_id: str | None = None, tags: list[str] | None = None, **kw: Any):
        try:
            from langfuse import propagate_attributes

            # Synchronous block: sets trace-level user and tags on the root span only.
            with propagate_attributes(user_id=hash_user(user_id), tags=tags or [], trace_name=name):
                raw = self._lf.start_observation(name=name, as_type="span", **kw)
            return _Span(raw)
        except Exception:
            log.warning("tracing: start failed", exc_info=True)
            return NullSpan()

    def score(self, trace_id: str, name: str, value: float, comment: str | None = None) -> None:
        try:
            self._lf.create_score(name=name, value=float(value), trace_id=trace_id, comment=comment)
        except Exception:
            log.warning("tracing: score failed", exc_info=True)

    def flush(self) -> None:
        try:
            self._lf.flush()
        except Exception:
            log.warning("tracing: flush failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            self._lf.shutdown()
        except Exception:
            log.warning("tracing: shutdown failed", exc_info=True)


def error_fields(e: BaseException) -> dict[str, Any]:
    """Fields for span.update(): class name as status, message in metadata (PII-masked by the SDK mask)."""
    return {
        "level": "ERROR",
        "status_message": type(e).__name__,
        "metadata": {"error_type": type(e).__name__, "error_message": str(e)[:500]},
    }


@contextmanager
def span(parent, name: str, as_type: str = "span", **kw: Any) -> Iterator[Any]:
    """Child span that always ends and records an exception as an ERROR before re-raising."""
    s = parent.child(name, as_type=as_type, **kw)
    try:
        yield s
    except BaseException as e:
        if not isinstance(e, (GeneratorExit, KeyboardInterrupt)):
            s.update(**error_fields(e))
        raise
    finally:
        s.end()
