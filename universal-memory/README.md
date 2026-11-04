# universal-memory

Standalone adaptive memory for Node.js. One class, one durable JSONL store, zero dependencies.

Built for assistant-style memory rather than a plain vector cache: it deduplicates, versions contradictory facts, reinforces what gets used, decays different memory kinds at different rates, assembles bounded context, and consolidates old episodes.

## Features

- **Document RAG** — chunked `ingestDocument()` / `ingestFile()` / `ingestUrl()`, native PDF and DOCX/XLSX/PPTX text extraction, HTML extraction, OCR hooks, document listing/removal, and chunk citations.
- **Encrypted + layout PDFs** — password support via Poppler, and `-layout` column/table preservation.
- **Multilingual OCR** — Tesseract language packs (`eng+deu`, etc.) with DPI/config/preprocess controls.
- **Async embedding** — optional background queue with retries and `flushEmbeddings()`.
- **Metrics** — operation counters, embed failures, queue depth, and recall hit/miss.
- **Drop-in HTTP sidecar** — `universal-memory/server` exposes remember/recall/context/documents over JSON for any language.
- **Encryption at rest** — AES-256-GCM JSONL with a passphrase or 32-byte key.
- **Auth + audit hooks** — `authorize(action, context)` ACL checks and `onEvent` audit events.
- **Hybrid retrieval** — BM25 plus Ollama embeddings, fused with reciprocal-rank fusion. Works with current `/api/embed` and legacy `/api/embeddings`.
- **ANN above 20k** — deterministic IVF index engages automatically for large stores, with exact search for smaller or scoped queries, plus optional persisted IVF sidecars.
- **Key rotation** — AES-256-GCM stores can be rekeyed atomically without losing data.
- **Hardened HTTP sidecar** — auth, rate limiting, CORS, request IDs, optional TLS, pagination, and metrics.
- **Ingestion extraction** — `ingest()` turns raw turns into structured facts/preferences using a custom extractor, an Ollama generation model, or an offline heuristic.
- **Labeled evaluation** — a 15-case dataset measures recall@5, MRR, stale facts, duplicates, document retrieval, and context token cost.
- **Adaptive salience** — base importance, access reinforcement, and confidence; not a fixed 0.5 forever.
- **Type-aware decay** — turns fade in days, facts in a year, preferences in years.
- **Dedupe and merge** — exact and semantic near-duplicates reinforce the existing entry.
- **Contradiction versioning** — facts/preferences keep `validFrom` / `validTo` / `supersedes` history.
- **Context assembly** — `context()` returns a token-budgeted, MMR-diversified, cited block; budgets adapt to the model window, from 4k to 1M+.
- **Consolidation** — `consolidate()` groups old episodes and archives them into summaries.
- **Namespaces, sessions, TTL** — scope memory without separate files.
- **Durable storage** — schema marker, fsync on append, atomic compaction, append/compaction lock, optional checksums.
- **Optional SQLite backend** — built on Node's own `node:sqlite` for WAL/transactions, no dependency added.
- **Governance** — feedback, purge, export/import, query cache, optional reranker.
- **Pluggable storage** — inject any sync adapter; JSONL is the default.

## Install

```bash
npm install universal-memory
```

For semantic retrieval, install Ollama and pull the default model:

```bash
ollama pull nomic-embed-text
```

## Quick start

```js
const UniversalMemory = require('universal-memory');

const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  namespace: 'default',
  ollamaUrl: 'http://127.0.0.1:11434',
  model: 'nomic-embed-text',
});

await memory.remember('User prefers dark mode in the editor', {
  kind: 'preference',
  subject: 'user',
  predicate: 'editor.theme',
  value: 'dark',
  importance: 0.9,
  confidence: 0.9,
});

const hits = await memory.recall('editor theme', { k: 5 });
const ctx = await memory.context('editor theme', { tokenBudget: 1200 });
```

If Ollama is offline, writes still succeed with `embedded: false`, recall falls back to BM25, and `.reindex()` fills vectors later.

## Memory model

| Kind | Purpose | Default decay |
|---|---|---|
| `turn` | Raw conversation turn | 7 days |
| `episode` | Event or action | 30 days |
| `note` | General free-form memory | 30 days |
| `fact` | Durable claim, optionally structured | 365 days |
| `preference` | Durable preference, optionally structured | 3650 days |
| `summary` | Consolidated history | 365 days |
| `document` | Chunk of an ingested document | 365 days |

Structured entries can carry `subject`, `predicate`, `value`, `confidence`, `validFrom`, `validTo`, `supersedes`, and `ttlDays`.

## Constructor options

| Option | Default | Purpose |
|---|---|---|
| `file` (`storagePath` / `path`) | `./memory.jsonl` | JSONL path. |
| `storage` | JSONL adapter | Custom sync `{ load, append, rewrite, size }` backend. |
| `namespace` | `"default"` | Default write/recall namespace. |
| `session` | `null` | Optional default session filter. |
| `ollamaUrl` | `http://127.0.0.1:11434` | Ollama base URL, local or HTTPS remote. |
| `apiKey` | `null` | Bearer token for a remote Ollama-compatible endpoint. |
| `headers` | `{}` | Extra headers for embedding/generation requests. |
| `documentHeaders` | `{}` | Headers for `ingestUrl()` document fetches. |
| `documentParser` | `null` | Custom `(buffer, meta) => text` override; native PDF/Office text extraction is built in. |
| `ocr` | `null` | `(imageBuffer, meta) => text` OCR hook for images/scanned PDFs. |
| `pdfRenderer` | `null` | `(pdfBuffer, meta) => imageBuffers` for scanned PDFs with OCR. |
| `pdfTextExtractor` | Poppler | Text extractor for encrypted/layout PDFs. |
| `pdfPassword` | `''` | Password for encrypted PDFs. |
| `pdfLayout` | `false` | Use `pdftotext -layout` for columns/tables. |
| `pdftotextCommand` | `pdftotext` | Poppler text command/path. |
| `deferEmbedding` | `false` | Store immediately, embed through a background queue. |
| `embedRetries` | `1` | Retries for failed deferred embeddings. |
| `autoResumeEmbeddings` | `false` | Queue missing vectors when the store opens. |
| `documentTimeoutMs` | `30000` | URL document fetch timeout. |
| `model` (`embedModel`) | `nomic-embed-text` | Embedding model tag. |
| `embed` | Ollama client | Custom `texts => vectors`; `null`/`false` forces keyword-only. |
| `fetch` | `globalThis.fetch` | Fetch implementation for Ollama. |
| `weights` | `{ relevance: 0.6, recency: 0.25, importance: 0.15 }` | Scoring weights. |
| `decayDays` | type-aware table | Number or `{ turn, episode, note, fact, preference, summary, default }`. |
| `alias` | ticker map | Replacement alias map; `{}` disables aliases. |
| `aliases` | ticker map | Additive alias entries over defaults. |
| `defaultImportance` | `0.5` | Fallback importance. |
| `defaultConfidence` | `0.5` | Fallback confidence. |
| `dedupe` | `true` | Merge near-duplicates on write. |
| `dedupeSimilarity` | `0.92` | Cosine floor for semantic dedupe. |
| `dedupeKinds` | `note, fact, preference` | Kinds eligible for merge-on-write. |
| `contradictionPolicy` | `"version"` | `version`, `reject`, or `keep` for structured facts/preferences. |
| `reinforceOnRecall` | `true` | Increment access counts on recall. |
| `feedbackStep` | `0.05` | Importance delta for `feedback()`. |
| `rerank` | `null` | Optional async reranker `({ query, candidates }) => ranked`. |
| `rerankCandidates` | `24` | Candidates passed to the reranker. |
| `summarizer` | `null` | Optional async `(texts, entries) => summary`. |
| `extractor` | `null` | Custom `(text, context) => candidates` for `extract()` / `ingest()`. |
| `extractModel` | `null` | Ollama generation model used when no custom extractor is set. |
| `generate` | `null` | Custom `(prompt, options) => text` generator for extraction. |
| `extractTimeoutMs` | `120000` | Generation timeout for `extractModel`. |
| `heuristicExtractor` | `true` | Offline extraction fallback for common durable statements. |
| `maxExtracted` | `8` | Maximum candidates returned per extraction. |
| `countTokens` | `chars / 4` | Token estimator for context budgets. |
| `salience` | built-in | Optional `(entry) => 0..1` override. |
| `minSimilarity` | `0.3` | Cosine floor for semantic hits. |
| `queryCacheSize` | `100` | Query embedding LRU size. |
| `annThreshold` | `20000` | Vector count that enables the IVF index; `0` disables. |
| `annNProbe` | `8` | IVF clusters scanned per query. |
| `annClusters` | `0` (auto) | IVF cluster count; `0` uses `sqrt(n)`. |
| `annRebuildAfter` | `1000` | Added vectors before an IVF rebuild. |
| `annPersist` | `false` | Persist/restore the IVF sidecar (`true` or a path). |
| `contextTokenBudget` | `2000` | Default `context()` token budget. |
| `contextK` | `8` | Default `context()` entry count. |
| `contextMmr` | `true` | Default MMR diversity. |
| `modelContextWindow` | `null` | Enables adaptive budgets from the model window. |
| `contextBudgetRatio` | `0.5` | Fraction of usable window used for memory. |
| `contextReserveTokens` | `1024` | Tokens reserved for prompt/output. |
| `contextMaxTokens` | `inf` | Optional hard cap on adaptive budget. |
| `ann` | `true` | Set `false` to force exact vector search. |
| `timeoutMs` | `20000` | Ollama request timeout. |
| `batchSize` | `16` | Embedding batch size. |
| `maxTurns` | `2000` | Turns kept during compaction. |
| `compactBytes` | `5 MiB` | Compaction size trigger. |
| `fsync` | `true` | fsync append/rewrite. |
| `writeLock` | `true` | Lock appends and compaction; `false` is faster for a single writer. |
| `checksums` | `false` | Add and verify per-record `chk`. |
| `encryptionKey` | `null` | Encrypt JSONL at rest using AES-256-GCM. |
| `onEvent` | `null` | Audit event callback. |
| `authorize` | `null` | ACL hook `(action, context) => boolean`. |
| `lockStaleMs` | `30000` | Compaction lock staleness. |
| `lockWaitMs` | `1500` | Compaction lock wait. |
| `now` | `Date.now` | Clock injection. |

A string argument is shorthand for `{ file }`.

## API

### `await remember(text, options)`

Options: `kind`, `tags`, `src`, `at`, `importance`, `namespace`, `session`, `subject`, `predicate`, `value`, `confidence`, `validFrom`, `validTo`, `ttlDays`, `dedupe`.

```js
const result = await memory.remember('User prefers dark mode', {
  kind: 'preference',
  subject: 'user',
  predicate: 'editor.theme',
  value: 'dark',
  importance: 0.9,
});
// { id, entry, embedded, merged?, superseded? } or { error, conflicts? }
```

- Exact and semantic duplicates merge into the existing entry (`merged: true`, `seen` increments, importance increases).
- A conflicting structured fact/preference with `contradictionPolicy: "version"` archives the old value (`validTo`, `supersededBy`) and sets `supersedes` on the new one.
- `"reject"` returns `{ error: "CONTRADICTION", conflicts }`; `"keep"` stores both.
- `ttlDays` sets `validTo`; expired entries remain in history but leave active recall.

### `await recall(query, options)`

Options: `k`, `kinds`, `namespace`, `session`, `includeHistory`, `reinforce`, `minScore`, `rerank`, `route`.

```js
const hits = await memory.recall('editor theme', { k: 5 });
```

Each hit includes the entry plus `score`, `relevance`, `recency`, and `importanceScore`. `memory.lastMode` is `hybrid`, `hybrid-partial`, or `lexical`. `route: true` uses `inferKinds(query)` when `kinds` is omitted.

### `await context(query, options)`

Options: `tokenBudget` (default 2000), `k` (default 8), `mmr` (true), `mmrLambda` (0.7), `perSubject` (1), plus recall options.

```js
const { text, entries, tokens, truncated, citations } = await memory.context('editor theme', {
  tokenBudget: 1200,
});
```

For a genuinely large context, set it globally:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  modelContextWindow: 128000, // model window
});

const ctx = await memory.context(query);
```

The memory adapts automatically:

| Model window | Default memory budget |
|---|---|
| 4k | 1,536 |
| 8k | 3,488 |
| 32k | 15,872 |
| 128k | 63,488 |
| 1M | 499,488 |
| 2M | 999,488 |

It reserves 1,024 tokens for system/output and uses 50% of the usable window. Tune with `contextBudgetRatio`, `contextReserveTokens`, and `contextMaxTokens`.

```js
memory.contextBudgetFor(8000); // 3488
await memory.context(query, { modelContextWindow: 8000 });
```

An explicit `tokenBudget` or `contextTokenBudget` overrides adaptive sizing. `contextK` auto-scales, and MMR is disabled automatically above 200 entries unless you explicitly set it.

A 1M-token block only works if your model supports a 1M window. It is **not** a good default: it is expensive, slow, and unnecessary when retrieval can select the right subset.

`text` is a cited block like:

```text
Memory context:
- [m_abc123] (preference, user.editor.theme, confidence 0.90) User prefers dark mode
```

MMR reduces near-duplicate context, `perSubject` caps one active value per `subject.predicate`, and the budget is enforced with `countTokens`.

### `await ingest(text, options)`

Store a raw turn **and** extract durable facts/preferences from it.

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  extractModel: 'llama3.2', // any pulled Ollama generation model
});

await memory.ingest('I prefer dark mode in the editor and my name is Alex.');
// { turn, extracted: [id1, id2], candidates, errors }
```

Extraction precedence:

1. `options.extractor` or constructor `extractor(text, context)`.
2. `extractModel` (or custom `generate`) through Ollama `/api/generate`.
3. Offline heuristic extractor.

The offline heuristic recognizes common durable statements (`I prefer…`, `My name is…`, `I live in…`, `I work at…`, `I'm allergic to…`, `Remember that…`) and maps themes, languages, diets, and locations to structured predicates.

```js
// No generation model required:
await memory.ingest('I live in Berlin and I prefer Python.');
```

`ingest(..., { extract: false })` stores only the raw turn. Extracted entries are normal memories: deduped, versioned, scored, and searchable.

### `await extract(text, options)`

Preview candidates without storing anything:

```js
const candidates = await memory.extract('I prefer dark mode and live in Tokyo');
// [{ kind, text, subject, predicate, value, confidence, importance }, ...]
```

Custom extractor:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  extractor: async (text) => [
    { kind: 'fact', text: 'User works at Acme', subject: 'user', predicate: 'employer', value: 'Acme' },
  ],
});
```

`consolidate({ extract: true })` also extracts durable candidates from old episode groups before archiving them.

### Maintenance and governance

| Method | Description |
|---|---|
| `feedback(id, { useful, delta })` | Raise/lower stored importance. |
| `forget(id)` | Tombstone an entry. |
| `history(subject, predicate, opts)` | All versions, oldest first. |
| `get(id)` | Entry or `null`. |
| `recent(n, opts)` / `turns(n, opts)` | Chronological views. |
| `count(kinds, opts)` / `namespaces()` / `stats(ns)` | Inventory. |
| `dedupe(opts)` | Merge historical near-duplicates. |
| `consolidate(opts)` | Summarize old episodes into a `summary`, archive originals. |
| `reindex()` | Backfill missing/stale vectors. |
| `ingestDocument()` / `ingestFile()` / `ingestUrl()` | Chunk and store documents. |
| `documents()` / `removeDocument(docId)` | List/remove document chunk sets. |
| `purge(filter)` | Hard-delete matching entries. |
| `export(opts)` / `import(data, opts)` | Backup/restore including vectors and history. |
| `compact()` | Rewrite storage, remove tombstones, trim turns. |
| `clearQueryCache()` | Drop cached query embeddings. |

## Scoring

```text
decay = decayDays[kind]
recency = exp(-age / decay)

usage = min(1, log1p(accessCount) / log1p(20))
salience = clamp(importance + 0.10 * usage + 0.05 * (confidence - 0.5))

score = 0.60 * (relevance / topRelevance)
      + 0.25 * recency
      + 0.15 * salience
```

Weights, decay windows, and `salience` are constructor options. Access counts update on recall; `feedback()` adjusts base importance.

## ANN retrieval

Below `annThreshold` (default 20,000 vectors), semantic search is exact. Above it, a deterministic IVF index is built lazily:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  annThreshold: 20000, // 0 disables ANN
  annNProbe: 8,        // clusters scanned per query
  annPersist: true,    // sidecar at <file>.ann
  annClusters: 0,      // 0 = sqrt(n)
  annRebuildAfter: 1000,
});

memory.stats().ann;
// { size: 24000, clusters: 154, nprobe: 8 }
```

Scoped queries that would return too few ANN candidates automatically fall back to exact search. Set `ann: false` or `annThreshold: 0` to force exact search; tune `annNProbe` against your own recall target. The evaluation suite includes an ANN recall check (~0.87 recall@10 at `nprobe: 12` on 1,000 synthetic vectors). With `annPersist: true`, the IVF centroids/buckets are stored at `<file>.ann`; a matching model/ID fingerprint restores them on startup, and `saveAnn()` flushes manually.

## Dedupe, versioning, TTL

```js
// Merge by exact text or cosine >= dedupeSimilarity
await memory.remember('Project uses tabs');
await memory.remember('Project uses tabs'); // same id, seen=2

// Structured contradiction
await memory.remember('Dark mode', { kind: 'preference', subject: 'user', predicate: 'theme', value: 'dark' });
await memory.remember('Light mode', { kind: 'preference', subject: 'user', predicate: 'theme', value: 'light' });
// dark.validTo is set; new entry.supersedes = dark id
memory.history('user', 'theme'); // [dark, light]

// TTL
await memory.remember('Temporary token', { ttlDays: 1 });
```

## Consolidation

```js
const result = await memory.consolidate({
  olderThanDays: 30,
  kinds: ['turn', 'episode'],
  similarity: 0.85,
  summarizer: async (texts) => `Summary: ${texts.join(' / ')}`,
});
// { groups, summaries, archived, dryRun }
```

Without a summarizer, unique texts are joined. Originals get `validTo` and `consolidatedInto`; the summary is active and searchable. `dryRun: true` previews groups.

## JSONL schema

```json
{"t":"schema","v":2}
{"t":"add","id":"m_...","at":"ISO","kind":"preference","text":"...","tags":[],"src":"...",
 "namespace":"...","subject":"user","predicate":"editor.theme","value":"dark",
 "confidence":0.9,"validFrom":"ISO","validTo":null,"supersedes":null,
 "seen":1,"accessCount":0}
{"t":"imp","id":"m_...","importance":0.9}
{"t":"meta","id":"m_...","fields":{"accessCount":1,"lastAccessedAt":"ISO"}}
{"t":"vec","id":"m_...","vec":"<base64 float32>","model":"nomic-embed-text"}
{"t":"del","id":"m_...","at":"ISO"}
```

- `schema` is written first on new/rewritten storage, marking format version 2.
- `add` keeps the original core fields; metadata is additive.
- `imp` and `meta` carry mutable state; `vec` carries embeddings.
- Deletes are tombstones until `compact()`.
- With `checksums: true`, each record gains a short `chk`; invalid records are skipped.

## Durability and storage

- Appends use `O_APPEND` plus `fsync` by default.
- A `.lock` file serializes appends and compaction; stale locks are recovered after `lockStaleMs`.
- `checksums` detects tampered/truncated rows.
- Custom storage must be synchronous:

```js
const storage = {
  file: null,
  load: () => records.slice(),
  append: (record) => records.push(record),
  rewrite: (next) => { records.length = 0; records.push(...next); },
  size: () => JSON.stringify(records).length,
};
const memory = new UniversalMemory({ storage });
```

With the JSONL backend, appends and compaction are serialized through a `.lock` file, so multiple processes do not interleave writes. SQLite adds transactional multi-process safety. This is still a single-node memory store, not a distributed database.

### Optional SQLite backend

On Node 22.5+, use the built-in SQLite backend for WAL, transactions, and multi-process safety:

```js
const { UniversalMemory, SqliteStorage } = require('universal-memory');

const storage = new SqliteStorage('./data/memory.sqlite');
const memory = new UniversalMemory({ storage });
await memory.remember('SQLite-backed memory');
// ...
memory.close();
```

`SqliteStorage` stores the same record stream in a single table and implements the same `load / append / rewrite / size` contract. `node:sqlite` is loaded lazily, so JSONL continues to work on older Node versions.

## Offline / local assistant

`universal-memory` is the memory layer, not the model. It works fully offline with a local generation model:

```text
user input
   │
   ├─ ingest() ──► turn + extracted facts/preferences
   │
   ├─ context() ─► token-budgeted, cited memory block
   │
   └─ local model (Ollama / llama.cpp / MLX)
          │
          └─ reply ──► remember() as a turn
```

A runnable example is in [examples/offline-assistant.js](examples/offline-assistant.js):

```bash
ollama pull nomic-embed-text
ollama pull llama3.2
MEMORY_FILE=./data/chat.jsonl node examples/offline-assistant.js
```

The key loop:

```js
await memory.ingest(userText);                          // store + extract
const context = await memory.context(userText, { tokenBudget: 1500 });
const reply = await localModel(context.text, userText); // your local LLM
await memory.remember(reply, { kind: 'turn', importance: 0.2 });
```

This is suitable for a single-user offline assistant, a desktop copilot, or a local voice assistant. For multi-user or multi-device deployments, use namespaces plus the SQLite backend and add authentication/encryption at the application layer.

## Online / remote endpoints

The same API works against a hosted Ollama-compatible service over HTTPS:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  ollamaUrl: 'https://memory-api.example.com/ollama',
  apiKey: process.env.MEMORY_API_KEY, // sent as Authorization: Bearer …
  headers: { 'x-project': 'assistant' },
  model: 'nomic-embed-text',
  extractModel: 'llama3.2',
});
```

`apiKey` and `headers` apply to embeddings and generation requests. The file path can stay local (recommended) or use the SQLite backend on the server.

For OpenAI-compatible or other providers, inject adapters instead of changing the memory API:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  embed: async (texts) => {
    const result = await openai.embeddings.create({ model: 'text-embedding-3-small', input: texts });
    return result.data.map((item) => item.embedding);
  },
  generate: async (prompt) => {
    const result = await openai.chat.completions.create({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: prompt }],
    });
    return result.choices[0].message.content;
  },
});
```

Use `context()` with any model SDK; the memory layer is provider-agnostic. Keep API keys and memory files server-side in a web deployment.

## Document RAG

`universal-memory` now includes the document ingestion half as well:

```js
const doc = await memory.ingestDocument(handbookText, {
  title: 'Engineering Handbook',
  source: 'handbook.md',
  chunkSize: 1200,
  chunkOverlap: 200,
  metadata: { team: 'platform' },
});

await memory.ingestFile('./docs/guide.html');
await memory.ingestUrl('https://docs.example.com/guide');
```

- Text, Markdown, JSON, HTML, **text-based PDFs**, and **DOCX/XLSX/PPTX** are built in.
- PDF extraction is native: zlib/Flate streams, object streams, page trees, and `Tj`/`TJ` text operators.
- Office extraction is native: OOXML zip parsing plus XML text/shared-string extraction.
- DOCX/PPTX table cells become tab-separated rows; XLSX rows are tab-separated.
- Encrypted PDFs use Poppler `pdftotext` with `pdfPassword`; layout-preserving extraction uses `pdfLayout: true`.
- Scanned PDFs and images need an OCR engine:

```js
const { UniversalMemory, tesseractOcr, popplerRenderer } = require('universal-memory');

const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  ocr: tesseractOcr({ language: 'eng', psm: 6, dpi: 300 }),
  pdfRenderer: popplerRenderer({ dpi: 300, grayscale: true }),
});
```

- Text-based PDFs do **not** need OCR or a parser hook.
- Scanned PDFs require `pdfRenderer` to turn pages into images, then `ocr`.
- `popplerRenderer()` wraps a locally installed Poppler `pdftoppm` binary.
- `popplerTextExtractor()` wraps `pdftotext` for encrypted and layout-preserving PDFs.
- Encrypted PDFs:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  pdfPassword: process.env.PDF_PASSWORD,
  pdfLayout: true,
});
await memory.ingestFile('./encrypted.pdf');
```

- Multilingual OCR uses Tesseract language packs:

```js
ocr: tesseractOcr({ language: 'eng+deu' })
```

Install the matching traineddata (`deu`, `fra`, `hin`, ...) in Tesseract's `tessdata` directory. The package does not bundle language packs.

Image OCR is the same hook without a renderer:

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  ocr: tesseractOcr({ language: 'eng', psm: 6, dpi: 300 }),
});

await memory.ingestFile('./receipt.png');
await memory.ingestUrl('https://example.com/scan.jpg');
```

Use `preprocess` for deskew/denoise/threshold cleanup:

```js
ocr: tesseractOcr({
  preprocess: async (image) => await cleanImage(image), // your image pipeline
})
```
- Images (`.png`, `.jpg`, `.jpeg`, `.webp`, `.tif`, `.bmp`) use the `ocr` hook directly — no PDF renderer needed.
- A custom `documentParser` still overrides native extraction.
- `tesseractOcr()` supports `psm`, `dpi`, Tesseract `config`, and an image `preprocess` hook.
- `popplerRenderer()` supports `dpi`, `grayscale`, and `format`; defaults are 300 DPI PNG output.
- 300 DPI + grayscale is the recommended starting point for scanned text.
- `tesseractOcr()` calls a locally installed Tesseract binary; no framework dependency and no npm package.
- Each chunk is a `document` entry linked by `docId`, `title`, `chunk`, and `metadata`.
- `memory.documents()` lists documents; `memory.removeDocument(docId)` removes all chunks.
- `recall(query, { docId })` or `context(query, { docId })` scopes retrieval to one document.
- Citations include `doc:Title#chunk`.
- `ingestUrl()` uses `documentHeaders` and never sends Ollama credentials to the document URL.

## Encryption at rest

```js
const memory = new UniversalMemory({
  file: './data/memory.enc',
  encryptionKey: process.env.MEMORY_KEY, // passphrase or 32-byte Buffer
});
```

AES-256-GCM encrypts every record. The file contains only a plaintext header with a random salt; wrong keys fail with `BAD_ENCRYPTION_KEY`. Works with the same recall/context/compaction APIs.

Rotate the key without losing data:

```js
memory.rotateEncryptionKey(process.env.NEW_MEMORY_KEY);
```

The store is decrypted with the current key and rewritten with a fresh salt/new key atomically. The old key stops working immediately; reopen with the new key.

## Auth and audit hooks

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  authorize: (action, context) => {
    return context.namespace === `tenant-${currentTenant}`;
  },
  onEvent: (event) => auditLog.write(event),
});
```

`authorize` is checked for remember, recall, forget, export, import, purge, document ingestion, and maintenance actions; denied operations throw `FORBIDDEN`. `onEvent` receives structured events such as `remember`, `recall`, `context`, `forget`, `feedback`, `ingest`, `consolidate`, `dedupe`, `purge`, and `import`.

## Async embeddings and metrics

```js
const memory = new UniversalMemory({
  file: './data/memory.jsonl',
  deferEmbedding: true,
  embedRetries: 2,
  autoResumeEmbeddings: true, // resume missing vectors after restart
});

const result = await memory.remember('Billing rollback notes');
// result.pending === true

await memory.resumeEmbeddings(); // explicit resume for an existing store
await memory.flushEmbeddings();  // waits for the background queue
memory.pendingEmbeddings();      // 0
memory.metrics();
// { remembers, recalls, contexts, documents, embedCalls,
//   embedFailures, embedRetries, pendingEmbeddings, memoryCount, ... }
```

Deferred mode keeps `remember()` fast and preserves lexical recall immediately. Semantic recall improves as vectors arrive. `flushEmbeddings()` is the consistency point for tests, shutdown, and batch jobs.

## PII and context boundaries

```js
const ctx = await memory.context(query, {
  format: 'xml',
  redactPII: true,
});

// <memory_context>
// Memory context:
// - [m_...] (note) Contact [EMAIL] about billing rollback
// </memory_context>
```

`redactPII` masks emails, phone numbers, SSNs, and card-like numbers in the returned context. `format: 'xml'` adds a clear data boundary for the model prompt. It reduces accidental prompt confusion, but is not a complete prompt-injection defense; treat retrieved text as untrusted data.

## Drop-in HTTP memory server

Use `universal-memory` from any language or process without embedding the class:

```bash
MEMORY_FILE=./data/memory.jsonl MEMORY_SERVER_KEY=secret npm run server
```

```js
const { UniversalMemory } = require('universal-memory');
const { createMemoryServer } = require('universal-memory/server');

const memory = new UniversalMemory({ file: './data/memory.jsonl' });
const server = createMemoryServer({
  memory,
  apiKey: process.env.MEMORY_SERVER_KEY,
  rateLimit: { windowMs: 60000, max: 120 },
  cors: true,
  // tls: { key: fs.readFileSync('key.pem'), cert: fs.readFileSync('cert.pem') },
});
server.listen(8787);
```

Server hardening:
- bearer/`x-api-key` auth
- per-IP token-window rate limiting (`429` + `retry-after`)
- CORS/preflight
- optional TLS
- `x-request-id`
- paginated `GET /documents?limit=20&offset=0`

| Route | Purpose |
|---|---|
| `GET /health` | Health and stats |
| `GET /metrics` | Operation/queue metrics |
| `POST /remember` | Store a memory |
| `POST /ingest` | Store + extract |
| `POST /recall` | Retrieve scored hits |
| `POST /context` | Build a cited context block |
| `POST /feedback` | Reinforce/penalize a memory |
| `GET/POST /documents` | List/ingest documents |
| `DELETE /documents/:docId` | Remove a document |
| `DELETE /memory/:id` | Forget a memory |

All routes except `/health` require `Authorization: Bearer <key>` when `apiKey` is set.

```bash
curl -X POST http://127.0.0.1:8787/recall \
  -H 'authorization: Bearer secret' \
  -H 'content-type: application/json' \
  -d '{"query":"billing rollback","k":5}'
```

## Migration guide

See [MIGRATION.md](MIGRATION.md) for replacing an existing memory implementation, mapping old methods, and wiring the context into OpenAI/Claude/local models.

## TypeScript

Types ship in `src/index.d.ts` and are wired through `package.json`.

## Environment doctor

```bash
npm run doctor
```

Checks Node, storage writability, Tesseract + installed languages, Poppler `pdftoppm`/`pdftotext`, and local Ollama/`nomic-embed-text`. Missing OCR/PDF engines are warnings, not failures.

## Tests and benchmark

```bash
npm test
npm run benchmark -- 10000 100
npm run evaluate
```

The suite covers CRUD, aliases, tombstones, compaction, legacy/BOM JSONL, checksums, custom storage, dedupe, versioning, TTL, namespaces/sessions, access feedback, scoring and decay, context budgets, consolidation, extraction, native PDF/Office, encrypted/layout PDF, OCR/multilingual/image, document ingestion, encryption/key rotation, auth/audit hooks, async embeddings, metrics, PII redaction/context boundaries, persisted ANN, HTTP server routes/rate limiting/CORS/pagination, export/import/purge, hybrid retrieval, query caching, reranking, ANN recall, both Ollama endpoints, and a labeled evaluation.

### Labeled evaluation

The dataset in `eval/dataset.json` covers preferences, profile facts, contradiction updates, duplicate suppression, recency, semantic synonyms, routing, TTL expiry, stale-fact rejection, and context size.

```bash
npm run evaluate
# {
#   "cases": 15,
#   "recallAtK": 1,
#   "mrr": 1,
#   "staleRate": 0,
#   "duplicateRate": 0,
#   "avgContextTokens": 25.7
# }

npm run evaluate:ollama
# same dataset against real local nomic-embed-text
```

The runner exits non-zero when recall@5, MRR, stale-fact, duplicate, or context-token thresholds regress.

### Real Ollama verification

```bash
ollama pull nomic-embed-text
OLLAMA_TEST=1 node test/rag.test.js
```

PowerShell:

```powershell
$env:OLLAMA_TEST = '1'; node test/rag.test.js
```

Set `OLLAMA_URL` / `OLLAMA_MODEL` for a non-default instance.

## Limits and extension points

- Vector search is exact below `annThreshold` and IVF-approximate above it; tune `annNProbe`/`annClusters` against your own recall target.
- Native PDF extraction covers text layers; scanned PDFs need OCR + page rendering. Encrypted PDFs need Poppler `pdftotext` + `pdfPassword`; complex columns/tables need `pdfLayout: true`. Office text/tables are native, but styles/images are not preserved.
- OCR languages require matching Tesseract `traineddata` files; the package does not bundle language packs.
- Encryption protects records at rest, not the running process or memory keys; use OS key management.
- JSONL uses an advisory lock; use the SQLite backend when you need transactional multi-process writes.
- Offline heuristic extraction is pattern-based; use `extractModel` or a custom `extractor` for open-ended language.
- ANN index is built lazily; `annPersist` avoids a full rebuild on restart. Budget one build pause above `annThreshold`. The sidecar stores vectors/IDs, not text, but it is not encrypted; protect it or disable `annPersist` in sensitive deployments.
- Reranking, summarization, extraction, salience, and storage are injectable hooks if you need larger models or a database-backed backend.
