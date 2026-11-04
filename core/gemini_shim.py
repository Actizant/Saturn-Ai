"""Gemini compatibility shim.

The repo is standardised on the new REST-based ``google-genai`` SDK
(``from google import genai``). A handful of older modules were still written
against the legacy ``google.generativeai`` ``GenerativeModel`` API, whose
package drags in the deprecated gRPC stack and an old ``protobuf`` that
downgrades the pinned one ``onnx`` needs.

This shim exposes the tiny slice of the legacy surface those modules use --
``model.generate_content(contents)`` returning an object with ``.text`` --
implemented on the modern client, so the call sites can stay untouched and no
legacy package has to be installed.
"""
from __future__ import annotations

from google import genai
from google.genai import types

from core.gemini_models import generate as _ladder_generate
from core.gemini_models import ladder as _model_ladder
from core.gemini_models import model_missing as _model_missing
from core.gemini_models import should_climb as _should_climb


def _normalise(item):
    """Coerce a legacy content piece into something the new client accepts.

    Handles the ``{"mime_type": ..., "data": ...}`` inline blobs and nested
    lists those modules pass; plain strings, PIL images and ``types.Part``
    objects are already supported by the new SDK and are returned as-is.
    """
    if isinstance(item, (list, tuple)):
        return [_normalise(x) for x in item]
    if isinstance(item, dict) and "data" in item:
        return types.Part.from_bytes(
            data=item["data"],
            mime_type=item.get("mime_type", "application/octet-stream"),
        )
    if isinstance(item, (bytes, bytearray)):
        return types.Part.from_bytes(data=bytes(item), mime_type="application/octet-stream")
    return item


class _Model:
    """Stands in for ``genai.GenerativeModel`` for the call sites we own."""

    def __init__(self, client, model_name, system_instruction=None):
        self._client = client
        # A single name is a single point of failure: callers still pass
        # retired literals from the 2.5 generation, so the ladder both
        # substitutes a living name and keeps the rest as retries.
        self._models = _model_ladder(model_name)
        self._model = self._models[0]
        self._system_instruction = system_instruction

    def generate_content(self, contents, **config):
        if self._system_instruction and "system_instruction" not in config:
            config["system_instruction"] = self._system_instruction

        # One loop for the whole app: the same climb the direct SDK callers use.
        return _ladder_generate(
            self._client,
            _normalise(contents),
            name=self._model,
            config=config or None,
        )


def model(api_key, model_name=None, system_instruction=None):
    """Build a legacy-shaped model bound to the modern ``google-genai`` client."""
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
    return _Model(client, model_name, system_instruction=system_instruction)
