"""Local-only, fail-soft client for the lobes senses gateway (stdlib ``urllib`` only).

Captured media (frames, audio) must never leave this host. Before the first
request for a role the client reads ``GET /capabilities`` and refuses with
``env.sense_not_local`` unless that payload *positively* shows the role served
here; anything it cannot parse is treated as "not local" (fail closed). An
unreachable gateway or a timeout is ``env.sense_unavailable``. 429/502/503 are
retried with bounded exponential backoff.

The ``/capabilities`` shape is unconfirmed (lobes#278), so it is parsed
defensively: a role is local only if its entry is a dict, is not marked
``proxied`` / ``hosted_by`` a peer, is ``loaded is True``, and (when replicas
are listed) at least one replica has ``local is True``.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess  # nosec B404
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from media_cli.media.errors import (
    ENV_SENSE_NOT_LOCAL,
    ENV_SENSE_UNAVAILABLE,
    INPUT_UNREADABLE,
    MediaEnvError,
    MediaInputError,
)

ENV_URL = "MEDIA_CLI_LOBES_URL"
ENV_KEY = "MEDIA_CLI_LOBES_KEY"
DEFAULT_URL = "http://localhost:8001"
CAPABILITIES_PATH = "/capabilities"
CHAT_PATH = "/v1/chat/completions"
TRANSCRIBE_PATH = "/v1/audio/transcriptions"

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
RETRY_STATUSES = frozenset({429, 502, 503})
LOBES_LOOKUP_TIMEOUT_SECONDS = 3.0
MAX_BODY_BYTES = 16 * 1024 * 1024

STT_ROLE = "stt"


def _clean_url(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    try:
        parsed = urlsplit(raw)
        parsed.port  # noqa: B018 - validates the port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return raw.rstrip("/")


def resolve_base_url(role: str) -> str:
    """MEDIA_CLI_LOBES_URL, else ``lobes endpoint <role> --json``, else localhost:8001."""
    env = _clean_url(os.environ.get(ENV_URL))
    if env:
        return env
    lobes = shutil.which("lobes")
    if lobes:
        try:
            proc = subprocess.run(  # nosec B603
                [lobes, "endpoint", role, "--json"],
                capture_output=True,
                text=True,
                timeout=LOBES_LOOKUP_TIMEOUT_SECONDS,
                check=False,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                found = _clean_url(data.get("endpoint")) if isinstance(data, dict) else None
                if found:
                    return found
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # fall through silently to the default
    return DEFAULT_URL


def _is_local(entry: object) -> bool:
    """True only if *entry* positively shows the role served on this machine."""
    if not isinstance(entry, dict):
        return False
    if entry.get("proxied") or entry.get("hosted_by"):
        return False
    if entry.get("loaded") is not True:
        return False
    replicas = entry.get("replicas")
    if isinstance(replicas, list) and replicas:
        return any(isinstance(r, dict) and r.get("local") is True for r in replicas)
    return True


def _unavailable(message: str, remediation: str = "") -> MediaEnvError:
    return MediaEnvError(
        ENV_SENSE_UNAVAILABLE,
        message,
        remediation or f"start the lobes gateway, or set {ENV_URL}",
    )


class SensesClient:
    """Talks to the lobes gateway; refuses any role not served on this machine."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._explicit_url = _clean_url(base_url) if base_url else None
        self._api_key = api_key if api_key is not None else (os.environ.get(ENV_KEY) or None)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        self._caps: dict[str, Any] | None = None
        self._verdicts: dict[str, str | None] = {}  # role -> served model (None = unknown)

    def __repr__(self) -> str:
        key = "set" if self._api_key else "unset"
        return f"SensesClient(url={self._explicit_url!r}, api_key=<{key}>)"

    # -- transport --------------------------------------------------------

    def _base(self, role: str) -> str:
        return self._explicit_url or resolve_base_url(role)

    def _request(
        self,
        role: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = self._base(role) + path
        attempt = 0
        while True:
            req = urllib.request.Request(  # nosec B310
                url, data=data, headers=headers, method="POST" if data is not None else "GET"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                    return resp.read(MAX_BODY_BYTES)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt < self.max_retries:
                    self._sleep(self._backoff(attempt, exc))
                    attempt += 1
                    continue
                raise _unavailable(f"senses gateway returned HTTP {exc.code} for {path}") from exc
            except (urllib.error.URLError, OSError) as exc:  # includes timeouts
                raise _unavailable(f"senses gateway unreachable at {url}: {_why(exc)}") from exc

    @staticmethod
    def _backoff(attempt: int, exc: urllib.error.HTTPError) -> float:
        delay = BASE_BACKOFF_SECONDS * (2**attempt)
        try:
            delay = max(delay, float(exc.headers.get("Retry-After", 0)))
        except (TypeError, ValueError, AttributeError):
            pass
        return min(delay, MAX_BACKOFF_SECONDS)

    # -- locality ---------------------------------------------------------

    def _capabilities(self, role: str) -> dict[str, Any]:
        if self._caps is None:
            raw = self._request(role, CAPABILITIES_PATH)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            self._caps = parsed if isinstance(parsed, dict) else {}
        return self._caps

    def ensure_local(self, role: str) -> None:
        """Raise unless ``/capabilities`` shows *role* served on this machine."""
        if role in self._verdicts:
            return
        entry = self._capabilities(role).get(role)
        if not _is_local(entry):
            raise MediaEnvError(
                ENV_SENSE_NOT_LOCAL,
                f"role {role!r} is not served on this machine; refusing to send media off-host",
                "run the role locally (lobes capabilities must show it loaded here, not proxied "
                "to a peer), or point MEDIA_CLI_LOBES_URL at a gateway that serves it",
            )
        model = entry.get("model")
        self._verdicts[role] = model if isinstance(model, str) else None

    def served_model(self, role: str = "senses") -> str | None:
        """The model id ``/capabilities`` reports for a local *role* (cache key for callers)."""
        self.ensure_local(role)
        return self._verdicts[role]

    # -- calls ------------------------------------------------------------

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        role: str = "senses",
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 512,
        temperature: float = 0,
    ) -> str:
        """POST a chat completion to *role*; returns the message content string."""
        self.ensure_local(role)
        payload: dict[str, Any] = {
            "model": role,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        raw = self._request(
            role,
            CHAT_PATH,
            data=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        try:
            content = json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError) as exc:
            raise _unavailable("senses gateway returned a malformed chat response") from exc
        if not isinstance(content, str):
            raise _unavailable("senses gateway returned a non-text chat response")
        return content

    def describe_images(
        self,
        paths: Sequence[str | Path],
        prompt: str,
        *,
        role: str = "senses",
        response_format: dict[str, Any] | None = None,
    ) -> str | dict[str, Any]:
        """Ask *role* about one or more local images.

        Returns the text, or the parsed JSON object when *response_format* is given.
        """
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for p in paths:  # read first: a bad path must not cost a network call
            parts.append(_image_part(Path(p)))
        content = self.chat(
            [{"role": "user", "content": parts}], role=role, response_format=response_format
        )
        if response_format is None:
            return content
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise _unavailable("senses model did not return valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _unavailable("senses model returned JSON that is not an object")
        return parsed

    def transcribe(self, wav_path: str | Path, *, language: str = "en") -> str:
        """Transcribe a local WAV via the ``stt`` role."""
        path = Path(wav_path)
        audio = _read(path)
        self.ensure_local(STT_ROLE)
        boundary = uuid.uuid4().hex
        body = b"".join(
            [
                _field(boundary, "language", language.encode()),
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                    f'filename="{path.name}"\r\nContent-Type: audio/wav\r\n\r\n'
                ).encode(),
                audio,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        raw = self._request(
            STT_ROLE,
            TRANSCRIBE_PATH,
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            text = json.loads(raw.decode("utf-8"))["text"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise _unavailable("senses gateway returned a malformed transcription") from exc
        if not isinstance(text, str):
            raise _unavailable("senses gateway returned a non-text transcription")
        return text


def _field(boundary: str, name: str, value: bytes) -> bytes:
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
    return head.encode() + value + b"\r\n"


def _why(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    return "timed out" if isinstance(reason, TimeoutError) else type(reason).__name__


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MediaInputError(
            INPUT_UNREADABLE, f"cannot read {path}: {exc.strerror or exc}", "check the path"
        ) from exc


def _image_part(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    b64 = base64.b64encode(_read(path)).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
