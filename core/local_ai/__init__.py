"""
core.local_ai - language edges for the (now cloud) brain.

The in-process HuggingFace stack that used to live here (the ResourceArbiter and
the LocalProvider that lazy-loaded a GGUF brain + Whisper ear + Kokoro mouth) was
removed when the assistant pivoted to a cloud API brain with no local ASR/TTS. The
only thing that survives is ``language_edges`` - the reply-language directive the
text path still uses - so this package no longer imports any arbiter/provider.
"""
