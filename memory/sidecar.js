// Saturn local memory sidecar. Started by memory/universal_bridge.py.
//
// The engine in ../universal-memory owns storage, ranking, import and export.
// This file only adds the routes the Saturn adapters need and that the
// engine server does not provide: a paged listing, a single-entry read, an
// engine diagnostics snapshot, extraction candidates (Stage Three needs
// candidates without storing them), document ingestion by path (the engine's
// own ingest routes take text that has already been extracted), and a
// namespace-scoped feedback route (the engine's is namespace-blind). Everything
// else (health, remember, ingest, recall, context, export, import, delete,
// documents, metrics) is delegated unchanged, so there is exactly one
// implementation of each memory operation.
//
// Two invariants are enforced here, and nowhere else:
//   * a snapshot is a (revision, contents) *pair*: reads that report a revision
//     share the write queue, and a write publishes its revision only after the
//     engine has committed, so no caller can observe a revision that names
//     contents which do not exist yet;
//   * every route that accepts an id enforces the requested namespace, because
//     the engine's per-id lookups are global.
'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const { UniversalMemory } = require('../universal-memory/src/index.js');
const { createMemoryServer } = require('../universal-memory/src/server.js');
const { tesseractOcr, popplerRenderer, popplerTextExtractor } = require('../universal-memory/src/ocr.js');

// One spelling for the engine's settings. The launcher in this repo and the
// bridge that spawns this file are renamed together, so a second, retired
// prefix here would only be a way for the old name to come back unnoticed.
function setting(name, fallback = '') {
  const value = process.env[`SATURN_${name}`];
  if (value !== undefined && value !== '') return value;
  return fallback;
}

const file = setting('MEMORY_FILE') || path.join(__dirname, '..', 'data', 'universal-memory.jsonl');
const port = Number(setting('MEMORY_PORT')) || 8799;
const host = setting('MEMORY_HOST', '127.0.0.1');
const key = process.env.SATURN_MEMORY_KEY ?? null;
const namespace = setting('MEMORY_NAMESPACE', 'saturn');
const startedAt = Date.now();
// Last failure this sidecar reported. Surfaced by /status so a supervisor can
// tell "idle" from "answering but degraded" without reading the logs.
let lastError = null;

const options = {
  file,
  namespace,
  fsync: true,
  writeLock: true,
  autoResumeEmbeddings: false,
};
// Store growth controls. The engine compacts the JSONL in place once it grows
// past ``compactBytes`` and takes a cross-process lock (with a stale timeout)
// around appends and compaction, so growth is bounded without this file
// re-implementing either behaviour.
const compactBytes = Number(setting('MEMORY_COMPACT_BYTES'));
if (Number.isFinite(compactBytes) && compactBytes > 0) options.compactBytes = Math.floor(compactBytes);
const lockStaleMs = Number(setting('MEMORY_LOCK_STALE_MS'));
if (Number.isFinite(lockStaleMs) && lockStaleMs > 0) options.lockStaleMs = Math.floor(lockStaleMs);

// The bridge decides whether Ollama is present (loopback probe). Absent,
// disabled, or unrequested means the engine runs its supported lexical mode
// instead of stalling writes on a socket that will never answer.
const embedMode = String(setting('MEMORY_EMBED', 'lexical')).toLowerCase();
if (embedMode === 'ollama') {
  options.ollamaUrl = setting('MEMORY_OLLAMA', 'http://127.0.0.1:11434');
  options.model = setting('MEMORY_MODEL', 'nomic-embed-text');
  const embedTimeout = Number(setting('MEMORY_EMBED_TIMEOUT_MS'));
  if (Number.isFinite(embedTimeout) && embedTimeout > 0) {
    options.timeoutMs = Math.min(embedTimeout, 30000);
  }
} else {
  options.embed = false;
}

// ── Document OCR ─────────────────────────────────────────────────────────────
//
// The engine treats OCR as an injected hook: with none, an image or a scanned
// PDF is refused with ``OCR_REQUIRED`` no matter what the host has installed.
// These hooks are what make document ingestion read anything at all. Both are
// driven by binaries rather than a model, so ingestion costs no tokens, no
// VRAM and no network call.
//
// A missing binary is not fatal here. Tesseract is only reached for a scan, and
// the hook raises ``OCR_ENGINE_UNAVAILABLE`` with its name, so the failure a
// user sees is "install Tesseract", not a generic 500. Plain-text PDFs and
// Office files never touch it.

// Resolve an external binary once, at startup. The Windows installers for
// Tesseract add nothing to PATH, so a bare command name would report "not
// found" on a machine where the engine is installed and working. Searching the
// conventional install directories is the difference between OCR that runs and
// an error asking the user to install what they already have.
const WINDOWS_TOOL_DIRS = process.platform === 'win32'
  ? ['ProgramFiles', 'ProgramFiles(x86)']
    .map((name) => process.env[name])
    .filter(Boolean)
    .map((base) => path.join(base, 'Tesseract-OCR'))
  : [];

function resolveBinary(name, extraDirs = []) {
  if (!name || path.isAbsolute(name)) return name;
  const extensions = process.platform === 'win32' ? ['.exe', '.cmd', ''] : [''];
  const dirs = String(process.env.PATH || '').split(path.delimiter).filter(Boolean).concat(extraDirs);
  for (const dir of dirs) {
    for (const extension of extensions) {
      try {
        if (fs.statSync(path.join(dir, name + extension)).isFile()) {
          return path.join(dir, name + extension);
        }
      } catch {
        /* not in this directory */
      }
    }
  }
  // Unresolved: hand the bare name to the engine, which reports the miss by
  // name instead of guessing at a path.
  return name;
}

const ocrLanguage = setting('OCR_LANG', 'eng');
const ocrPsm = Number(setting('OCR_PSM'));
options.ocr = tesseractOcr({
  command: resolveBinary(setting('TESSERACT', 'tesseract'), WINDOWS_TOOL_DIRS),
  language: ocrLanguage,
  psm: Number.isFinite(ocrPsm) && ocrPsm > 0 ? Math.floor(ocrPsm) : 6,
  dpi: 300,
});
options.pdfRenderer = popplerRenderer({
  command: resolveBinary(setting('PDFTOPPM', 'pdftoppm'), WINDOWS_TOOL_DIRS),
  dpi: 300,
  format: 'png',
});
// ``pdftotext -layout`` preserves column alignment; the engine's built-in PDF
// reader does not, so a two-column page would interleave into nonsense. When
// the binary is absent the extractor throws and the engine falls back to its
// own parser, so turning this on cannot make a PDF *worse*.
options.pdfLayout = true;
options.pdfTextExtractor = popplerTextExtractor({
  command: resolveBinary(setting('PDFTOTEXT', 'pdftotext'), WINDOWS_TOOL_DIRS),
  layout: true,
});

const memory = new UniversalMemory(options);
const engineServer = createMemoryServer({ memory, apiKey: key });
// The engine attaches its router as the single 'request' listener; reuse it
// rather than re-implementing any engine route.
const engineHandler = engineServer.listeners('request')[0];

// ── Compare-and-swap revision ────────────────────────────────────────────────
//
// The engine exposes no revision, and its ``export`` gives a caller no way to
// tell whether the store moved between that read and a later write. That is
// enough for two Saturn processes (or a stale in-process snapshot) to compute
// the same diff and let the slower writer erase the faster writer's success.
//
// The sidecar therefore mints a token: a per-process epoch plus a counter that
// every mutation bumps. The epoch makes a snapshot from a previous process
// unusable, and the counter makes ``POST /patch`` a real compare-and-swap -
// a stale caller gets 409 STALE_REVISION instead of applying an old diff.
const revisionEpoch = crypto.randomBytes(6).toString('hex');
let revisionCounter = 0;
const revisionOf = () => `${revisionEpoch}:${revisionCounter}`;

function bumpRevision() {
  revisionCounter += 1;
  return revisionOf();
}

// Mutations are applied one at a time, in arrival order, so a compare-and-swap
// check and the writes it guards can never interleave with another request.
// Snapshot reads share the same queue: a revision and the contents it names are
// only meaningful as a pair, so an export must never run between a write's
// commit and the revision that write publishes.
let mutationChain = Promise.resolve();

function serialized(task) {
  const run = mutationChain.then(task, task);
  mutationChain = run.then(() => undefined, () => undefined);
  return run;
}

function effectiveNamespace(requested) {
  const value = String(requested || '').trim();
  return value || namespace;
}

// An entry that carries no namespace was written before namespaces existed (or
// by a client that never set one). It belongs to this store and is readable;
// an entry that names a different namespace never is.
function namespaceMatches(entry, wanted) {
  if (!entry || typeof entry !== 'object') return false;
  const actual = String(entry.namespace || '').trim();
  return !actual || actual === wanted;
}

// How many entries this namespace holds, used to tell a failed import that
// committed part of its batch from one that committed nothing at all. ``null``
// means "cannot tell", which callers treat as a partial commit.
function entryCount(wanted) {
  try {
    const data = memory.export({ namespace: wanted });
    return Array.isArray(data && data.entries) ? data.entries.length : null;
  } catch {
    return null;
  }
}

function send(response, status, body) {
  // A queued task can fail after the route already answered - or answer from a
  // catch block after a partial commit. Writing twice would throw out of the
  // request handler and take the sidecar down with it, so a second answer is
  // dropped instead of resetting the connection.
  if (response.headersSent || response.writableEnded) return;
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
    'cache-control': 'no-store',
  });
  response.end(payload);
}

function authorized(request) {
  if (!key) return true;
  const header = String(request.headers.authorization || '');
  const bearer = header.startsWith('Bearer ') ? header.slice(7) : '';
  return bearer === key || request.headers['x-api-key'] === key;
}

function intParam(url, name, fallback) {
  const raw = Number(url.searchParams.get(name));
  return Number.isFinite(raw) && raw >= 0 ? Math.floor(raw) : fallback;
}

// POST routes that only read. Everything else that is not a GET changes the
// store and therefore moves the revision.
const NON_MUTATING_ROUTES = new Set(['/health', '/status', '/recall', '/context', '/extract']);

// Ten years. A TTL is a retention promise, not a timestamp: the engine coerces
// anything it cannot parse to "no expiry", which would silently keep a row the
// caller asked to expire. Out-of-range values are refused instead of guessed.
const MAX_TTL_DAYS = 3650;

function invalid(field, value, reason, hint) {
  const problem = { field, value: value === undefined ? null : value, reason };
  if (hint) problem.hint = hint;
  return problem;
}

// ``ttl`` is not an accepted alias. A caller that sends ``ttl: 1`` and gets a
// permanent row has been told the opposite of what it asked for, so the field
// is refused outright instead of being guessed at: say ``ttlDays``.
function ttlAliasProblem(payload, where) {
  if (payload.ttl === undefined) return null;
  return invalid(`${where}.ttl`, payload.ttl, 'UNSUPPORTED_TTL_ALIAS', 'send ttlDays');
}

// The engine coerces an unparseable TTL to "no expiry", which would keep a row
// the caller asked to expire; out-of-range values are refused, not guessed.
function ttlDaysProblem(payload, where) {
  if (payload.ttlDays === undefined || payload.ttlDays === null) return null;
  const value = payload.ttlDays;
  const days = Number(value);
  if (typeof value === 'boolean' || !Number.isFinite(days) || days <= 0 || days > MAX_TTL_DAYS) {
    return invalid(`${where}.ttlDays`, value, `MUST_BE_DAYS_ABOVE_0_UP_TO_${MAX_TTL_DAYS}`);
  }
  return null;
}

function unitRangeProblem(payload, where, field) {
  const value = payload[field];
  if (value === undefined || value === null) return null;
  const number = Number(value);
  if (typeof value === 'boolean' || !Number.isFinite(number) || number < 0 || number > 1) {
    return invalid(`${where}.${field}`, value, 'MUST_BE_NUMBER_0_1');
  }
  return null;
}

// One entry, validated the same way whether it arrives through ``/patch`` or
// ``/import``. ``imported`` adds the fields only a full record can carry.
function validateEntryFields(payload, where, { imported = false } = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return [invalid(where, payload, 'MUST_BE_OBJECT')];
  }
  const problems = [];
  if (typeof payload.text !== 'string' || !payload.text.trim()) {
    problems.push(invalid(`${where}.text`, payload.text, 'MUST_BE_NON_EMPTY_STRING'));
  }
  if (payload.kind !== undefined && (typeof payload.kind !== 'string' || !payload.kind.trim())) {
    problems.push(invalid(`${where}.kind`, payload.kind, 'MUST_BE_NON_EMPTY_STRING'));
  }
  if (payload.tags !== undefined && payload.tags !== null && !Array.isArray(payload.tags)) {
    problems.push(invalid(`${where}.tags`, payload.tags, 'MUST_BE_ARRAY'));
  }
  const timestampFields = imported ? ['at', 'validFrom', 'validTo'] : ['at'];
  timestampFields.forEach((field) => {
    const value = payload[field];
    if (value === undefined || value === null) return;
    if (typeof value !== 'string' || !value.trim()) {
      problems.push(invalid(`${where}.${field}`, value, 'MUST_BE_ISO_STRING'));
    }
  });
  const importance = unitRangeProblem(payload, where, 'importance');
  if (importance) problems.push(importance);
  if (imported) {
    if (payload.id !== undefined && payload.id !== null
      && (typeof payload.id !== 'string' || !payload.id.trim())) {
      problems.push(invalid(`${where}.id`, payload.id, 'MUST_BE_NON_EMPTY_STRING'));
    }
    const confidence = unitRangeProblem(payload, where, 'confidence');
    if (confidence) problems.push(confidence);
    if (payload.meta !== undefined && payload.meta !== null
      && (typeof payload.meta !== 'object' || Array.isArray(payload.meta))) {
      problems.push(invalid(`${where}.meta`, payload.meta, 'MUST_BE_OBJECT'));
    }
    if (payload.vec !== undefined && payload.vec !== null && typeof payload.vec !== 'string') {
      problems.push(invalid(`${where}.vec`, payload.vec, 'MUST_BE_BASE64_STRING'));
    }
  }
  const alias = ttlAliasProblem(payload, where);
  if (alias) problems.push(alias);
  const ttl = ttlDaysProblem(payload, where);
  if (ttl) problems.push(ttl);
  return problems;
}

// Validate a proposed patch *before* the queue is entered and before a single
// byte is mutated. Every problem is reported, so a caller can fix the whole
// request instead of discovering one bad field per round trip.
function validatePatch(body, writes, forgets) {
  const problems = [];
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return [invalid('body', body, 'MUST_BE_OBJECT')];
  }
  if (body.namespace !== undefined && body.namespace !== null && typeof body.namespace !== 'string') {
    problems.push(invalid('namespace', body.namespace, 'MUST_BE_STRING'));
  }
  if (body.remember !== undefined && !Array.isArray(body.remember)) {
    problems.push(invalid('remember', body.remember, 'MUST_BE_ARRAY'));
  }
  if (body.forget !== undefined && !Array.isArray(body.forget)) {
    problems.push(invalid('forget', body.forget, 'MUST_BE_ARRAY'));
  }
  writes.forEach((payload, index) => {
    problems.push(...validateEntryFields(payload, `remember[${index}]`));
  });
  forgets.forEach((raw, index) => {
    if (typeof raw !== 'string' || !raw.trim()) {
      problems.push(invalid(`forget[${index}]`, raw, 'MUST_BE_NON_EMPTY_STRING'));
    }
  });
  return problems;
}

// One request is one batch and one decision: it is validated whole, because
// the engine silently skips an entry it cannot parse and half an import is
// worse than none.
const MAX_IMPORT_ENTRIES = 5000;

// Ingestion reads the whole file into memory (and, for a scan, renders every
// page to an image), so the size ceiling is a memory bound, not a policy: a
// 200 MB PDF would exhaust the sidecar and take every queued write with it.
const MAX_INGEST_BYTES = 32 * 1024 * 1024;

function validateImport(body, entries) {
  const problems = [];
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return [invalid('body', body, 'MUST_BE_OBJECT')];
  }
  if (body.preserveIds !== undefined && typeof body.preserveIds !== 'boolean') {
    problems.push(invalid('preserveIds', body.preserveIds, 'MUST_BE_BOOLEAN'));
  }
  if (entries.length > MAX_IMPORT_ENTRIES) {
    problems.push(invalid('entries', entries.length, `MUST_BE_AT_MOST_${MAX_IMPORT_ENTRIES}`));
  }
  entries.forEach((entry, index) => {
    problems.push(...validateEntryFields(entry, `entries[${index}]`, { imported: true }));
  });
  return problems;
}

// Minimal body reader for the one POST route this file owns. The engine's own
// routes keep using the engine's reader, so this is not a second implementation
// of any memory operation - only of reading a small JSON request body.
function readJsonBody(request, limit = 256 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    let tooLarge = false;
    const chunks = [];
    request.on('data', (chunk) => {
      if (tooLarge) return; // keep draining; never reset the caller's socket
      size += chunk.length;
      if (size > limit) {
        // Stop buffering but read the rest of the body to its end, so the 413
        // that follows is a real HTTP answer on a live connection instead of a
        // reset that a client reports as a transport failure.
        tooLarge = true;
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (tooLarge) {
        const error = new Error('BODY_TOO_LARGE');
        error.code = 'BODY_TOO_LARGE';
        reject(error);
        return;
      }
      if (!chunks.length) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (error) {
        error.code = 'BAD_JSON';
        reject(error);
      }
    });
    request.on('aborted', () => {
      const error = new Error('REQUEST_ABORTED');
      error.code = 'REQUEST_ABORTED';
      reject(error);
    });
    request.on('error', (error) => {
      if (tooLarge) return; // the drain path owns the outcome
      reject(error);
    });
  });
}

const server = http.createServer(async (request, response) => {
  let url;
  try {
    url = new URL(request.url, 'http://localhost');
  } catch {
    return send(response, 400, { error: 'BAD_REQUEST' });
  }
  const pathname = url.pathname.replace(/\/+$/, '') || '/';

  try {
    // Authenticated, cheap liveness probe. It deliberately does not touch the
    // store, so a supervisor can poll it without paying for an export - and it
    // is answered here rather than by the engine, whose health route is
    // unauthenticated and would make a keyed sidecar look "open" to callers
    // that hold no token.
    if (pathname === '/health' && (request.method === 'GET' || request.method === 'HEAD')) {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      return send(response, 200, {
        ok: true,
        service: 'saturn-memory',
        mode: memory.lastMode,
        namespace,
      });
    }

    if (pathname === '/entries' && request.method === 'GET') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const wantedNamespace = effectiveNamespace(url.searchParams.get('namespace'));
      const includeHistory = url.searchParams.get('includeHistory') === '1';
      const kinds = url.searchParams.get('kinds');
      const wanted = kinds ? new Set(kinds.split(',').map((item) => item.trim()).filter(Boolean)) : null;
      // Queued with the writes: a listing must describe one committed state.
      return await serialized(() => {
        const exportData = memory.export({
          includeVectors: false,
          includeHistory,
          namespace: wantedNamespace,
        });
        let entries = (Array.isArray(exportData.entries) ? exportData.entries : [])
          .filter((entry) => namespaceMatches(entry, wantedNamespace));
        if (wanted) entries = entries.filter((entry) => wanted.has(entry.kind));
        const total = entries.length;
        const offset = intParam(url, 'offset', 0);
        const limit = intParam(url, 'limit', total);
        return send(response, 200, {
          ok: true,
          mode: memory.lastMode,
          namespace: wantedNamespace,
          total,
          offset,
          limit,
          entries: limit > 0 ? entries.slice(offset, offset + limit) : entries.slice(offset),
        });
      });
    }

    // Namespace-scoped export. The engine's own /export ignores the namespace
    // query, so a scoped read has to come from here: without it every profile
    // reconcile would index - and then delete - another namespace's rows.
    // Queued like the writes, so the revision it reports and the entries it
    // returns are always the same committed state.
    if (pathname === '/export' && request.method === 'GET') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const wantedNamespace = effectiveNamespace(url.searchParams.get('namespace'));
      return await serialized(() => {
        const exportData = memory.export({
          includeVectors: false,
          includeHistory: url.searchParams.get('includeHistory') === '1',
          namespace: wantedNamespace,
        });
        const entries = (Array.isArray(exportData.entries) ? exportData.entries : [])
          .filter((entry) => namespaceMatches(entry, wantedNamespace));
        return send(response, 200, {
          ...exportData,
          ok: true,
          mode: memory.lastMode,
          namespace: wantedNamespace,
          revision: revisionOf(),
          entries,
        });
      });
    }

    // Apply a diff computed from a specific snapshot. This is the only write
    // path a reconciler should use: the revision check makes an out-of-date
    // diff a 409 instead of a silent clobber.
    if (pathname === '/patch' && request.method === 'POST') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const body = await readJsonBody(request);
      const wantedNamespace = effectiveNamespace(body.namespace);
      const expected = String(body.expectedRevision || '');
      const writes = Array.isArray(body.remember) ? body.remember : [];
      const forgets = Array.isArray(body.forget) ? body.forget : [];
      // Validation happens before the queue and before any mutation: a refused
      // patch leaves the store byte-for-byte as it was, so the caller's
      // revision still describes reality and no half-applied diff is reported
      // as a stale one.
      const problems = validatePatch(body, writes, forgets);
      if (problems.length) {
        return send(response, 400, {
          error: 'INVALID_PATCH',
          namespace: wantedNamespace,
          revision: revisionOf(),
          details: problems,
        });
      }
      // Awaited inside the outer try/catch: a rejection from the queue is an
      // answer to this request, never an unhandled rejection that could take
      // the process (and every other connection) with it.
      return await serialized(async () => {
        if (!expected || expected !== revisionOf()) {
          return send(response, 409, {
            error: 'STALE_REVISION',
            revision: revisionOf(),
            namespace: wantedNamespace,
          });
        }
        const applied = [];
        const forgotten = [];
        const rejected = [];
        try {
          for (const payload of writes) {
            const result = await memory.remember(String(payload.text || ''), {
              ...payload,
              namespace: wantedNamespace,
            });
            if (!result || !result.id) {
              rejected.push({ reason: (result && result.error) || 'NOT_STORED', text: payload.text });
              continue;
            }
            applied.push(result.id);
          }
          for (const raw of forgets) {
            const id = String(raw || '');
            const entry = memory.get(id);
            if (!entry || !namespaceMatches(entry, wantedNamespace)) {
              rejected.push({ id, reason: 'NOT_FOUND' });
              continue;
            }
            memory.forget(id);
            forgotten.push(id);
          }
        } catch (error) {
          // A hard failure mid-diff stops the loop and reports exactly what
          // committed. The revision moves when anything did: a caller that
          // retries must not be handed a token claiming the store is unchanged.
          const revision = applied.length || forgotten.length ? bumpRevision() : revisionOf();
          lastError = String((error && error.message) || error);
          return send(response, 500, {
            error: 'PARTIAL_WRITE',
            namespace: wantedNamespace,
            revision,
            applied,
            forgotten,
            rejected,
            message: String((error && error.message) || error),
          });
        }
        return send(response, 200, {
          ok: true,
          namespace: wantedNamespace,
          revision: bumpRevision(),
          applied,
          forgotten,
          rejected,
          counts: {
            applied: applied.length,
            forgotten: forgotten.length,
            rejected: rejected.length,
          },
        });
      });
    }

    if (pathname === '/import' && request.method === 'POST') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const body = await readJsonBody(request);
      const wantedNamespace = effectiveNamespace(body.namespace);
      if (!Array.isArray(body.entries)) {
        return send(response, 400, { error: 'ENTRIES_REQUIRED', namespace: wantedNamespace });
      }
      // The whole batch is validated before the queue is entered: the engine
      // silently skips an entry it cannot parse, and an import that half
      // landed is worse than one that was refused.
      const problems = validateImport(body, body.entries);
      if (problems.length) {
        return send(response, 400, {
          error: 'INVALID_IMPORT',
          namespace: wantedNamespace,
          revision: revisionOf(),
          details: problems.slice(0, 50),
          counts: { problems: problems.length },
        });
      }
      // Destination-bound import: every entry is stamped with the destination
      // namespace, so a snapshot exported elsewhere cannot import into the
      // wrong store.
      const entries = body.entries.map((entry) => ({ ...entry, namespace: wantedNamespace }));
      return await serialized(async () => {
        const before = entryCount(wantedNamespace);
        try {
          const result = await memory.import(entries, {
            preserveIds: body.preserveIds !== false,
            namespace: wantedNamespace,
          });
          return send(response, 200, {
            ...(result || {}),
            ok: true,
            namespace: wantedNamespace,
            revision: bumpRevision(),
          });
        } catch (error) {
          // A throw can come after the engine committed part of the batch. The
          // revision has to describe what is actually in the store: a caller
          // that still holds the old one would compute a diff against contents
          // that no longer exist.
          const after = entryCount(wantedNamespace);
          const applied = before !== null && after !== null ? Math.max(0, after - before) : null;
          const message = String((error && error.message) || error);
          lastError = message;
          if (applied === null || applied > 0) {
            return send(response, 500, {
              error: 'PARTIAL_IMPORT',
              namespace: wantedNamespace,
              revision: bumpRevision(),
              applied,
              counts: { applied, total: entries.length },
              message,
            });
          }
          return send(response, 500, {
            error: 'IMPORT_FAILED',
            namespace: wantedNamespace,
            revision: revisionOf(),
            applied: 0,
            counts: { applied: 0, total: entries.length },
            message,
          });
        }
      });
    }

    if (pathname === '/diagnostics' && request.method === 'GET') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      return send(response, 200, {
        ok: true,
        namespace: memory.namespace,
        file: memory.file,
        mode: memory.lastMode,
        semantic: memory.semantic,
        model: memory.model || null,
        schemaVersion: memory.schemaVersion,
        stats: memory.stats(),
        metrics: memory.metrics(),
      });
    }

    // Operational health: one authenticated read that pairs the store revision
    // with everything a supervisor checks (mode, count, embeddings, last
    // error). Queued with the writes, so the count and revision describe the
    // same committed state instead of two different moments.
    if (pathname === '/status' && request.method === 'GET') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const wantedNamespace = effectiveNamespace(url.searchParams.get('namespace'));
      return await serialized(() => {
        let count = null;
        try {
          const data = memory.export({ includeVectors: false, namespace: wantedNamespace });
          count = Array.isArray(data && data.entries)
            ? data.entries.filter((entry) => namespaceMatches(entry, wantedNamespace)).length
            : null;
        } catch (error) {
          lastError = String((error && error.message) || error);
        }
        return send(response, 200, {
          ok: true,
          service: 'saturn-memory',
          pid: process.pid,
          startedAt: new Date(startedAt).toISOString(),
          uptimeMs: Date.now() - startedAt,
          mode: memory.lastMode,
          revision: revisionOf(),
          namespace: wantedNamespace,
          file: memory.file,
          schemaVersion: memory.schemaVersion,
          semantic: memory.semantic === true,
          model: memory.model || null,
          compactBytes: Number.isFinite(memory.compactBytes) ? memory.compactBytes : null,
          count,
          stats: memory.stats(),
          metrics: memory.metrics(),
          lastError,
        });
      });
    }

    if (pathname === '/extract' && request.method === 'POST') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const body = await readJsonBody(request);
      const text = String(body.text || '').trim();
      if (!text) return send(response, 400, { error: 'TEXT_REQUIRED' });
      const limit = Math.max(1, Math.min(Number(body.limit) || 5, 20));
      const candidates = await memory.extract(text, {
        namespace: body.namespace,
        session: body.session,
      });
      return send(response, 200, {
        ok: true,
        mode: memory.lastMode,
        candidates: (Array.isArray(candidates) ? candidates : []).slice(0, limit),
      });
    }

    // Feedback is a mutation, and the engine's route is namespace-blind: it
    // looks the id up in one global map, so a caller could reinforce - and
    // thereby keep alive - a row belonging to a store it will never read.
    // The namespace is enforced here, before the engine sees the id at all.
    if (pathname === '/feedback' && request.method === 'POST') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const body = await readJsonBody(request);
      const id = String(body.id || '');
      const wantedNamespace = effectiveNamespace(body.namespace);
      if (!id) return send(response, 400, { error: 'ID_REQUIRED', namespace: wantedNamespace });
      return await serialized(() => {
        const entry = memory.get(id);
        if (!entry || !namespaceMatches(entry, wantedNamespace)) {
          return send(response, 404, { error: 'NOT_FOUND', id, namespace: wantedNamespace });
        }
        const result = memory.feedback(id, {
          useful: body.useful !== false,
          delta: body.delta,
        });
        if (!result || result.error) {
          return send(response, 404, {
            ...(result || { error: 'NOT_FOUND' }),
            namespace: wantedNamespace,
            revision: revisionOf(),
          });
        }
        return send(response, 200, {
          ...result,
          ok: true,
          namespace: wantedNamespace,
          revision: bumpRevision(),
        });
      });
    }

    // The engine's own DELETE /documents/:docId is namespace-blind: it removes
    // every chunk that carries the doc id, in *every* namespace. This override
    // is authenticated, serialized, and passes the requested namespace into
    // ``removeDocument`` so one store cannot delete another's document - and a
    // doc id that belongs elsewhere is answered 404 instead of being deleted.
    if (pathname.startsWith('/documents/') && request.method === 'DELETE') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const docId = decodeURIComponent(pathname.slice('/documents/'.length));
      const body = await readJsonBody(request);
      const bodyNamespace = body && typeof body === 'object' ? body.namespace : null;
      const wantedNamespace = effectiveNamespace(url.searchParams.get('namespace') || bodyNamespace);
      if (!docId) {
        return send(response, 400, { error: 'DOC_ID_REQUIRED', namespace: wantedNamespace });
      }
      return await serialized(() => {
        const owned = (memory.documents(wantedNamespace) || [])
          .find((document) => document && document.docId === docId);
        if (!owned) {
          return send(response, 404, {
            error: 'NOT_FOUND',
            docId,
            namespace: wantedNamespace,
            revision: revisionOf(),
          });
        }
        const result = memory.removeDocument(docId, { namespace: wantedNamespace });
        const deleted = Number(result && result.deleted) || 0;
        if (!deleted) {
          return send(response, 404, {
            error: 'NOT_FOUND',
            docId,
            namespace: wantedNamespace,
            revision: revisionOf(),
          });
        }
        return send(response, 200, {
          ok: true,
          docId,
          title: owned.title || null,
          namespace: wantedNamespace,
          deleted,
          revision: bumpRevision(),
        });
      });
    }

    if (pathname.startsWith('/memory/') && request.method === 'GET') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const id = decodeURIComponent(pathname.slice('/memory/'.length));
      const wantedNamespace = effectiveNamespace(url.searchParams.get('namespace'));
      return await serialized(() => {
        const entry = memory.get(id);
        // The engine's get() is namespace-blind, so the scope check is here: a
        // single-record read must not leak (or later delete) another namespace.
        if (!entry || !namespaceMatches(entry, wantedNamespace)) {
          return send(response, 404, { error: 'NOT_FOUND', id });
        }
        return send(response, 200, { ok: true, namespace: wantedNamespace, entry });
      });
    }

    if (pathname.startsWith('/memory/') && request.method === 'DELETE') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const id = decodeURIComponent(pathname.slice('/memory/'.length));
      const wantedNamespace = effectiveNamespace(
        url.searchParams.get('namespace') || (await readJsonBody(request)).namespace,
      );
      return await serialized(() => {
        const entry = memory.get(id);
        if (!entry || !namespaceMatches(entry, wantedNamespace)) {
          return send(response, 404, { error: 'NOT_FOUND', id });
        }
        const result = memory.forget(id);
        return send(response, 200, {
          ...(result || {}),
          ok: true,
          namespace: wantedNamespace,
          revision: bumpRevision(),
        });
      });
    }

    // Document ingestion by path. The engine's own ``/ingest`` and ``/documents``
    // routes take text that the caller has already extracted, so a screenshot or
    // a scanned PDF cannot go through them: parsing happens inside the engine,
    // where the OCR hooks live. ``ingestFile`` is the only entry point that does
    // that, and it is library-only - this route is its transport.
    //
    // The answer carries the document's metadata and never its text, so this is
    // not a file reader: a caller that wants the words asks ``/recall``, which
    // applies the namespace and ranking rules like every other read.
    if (pathname === '/ingest/file' && request.method === 'POST') {
      if (!authorized(request)) return send(response, 401, { error: 'UNAUTHORIZED' });
      const body = await readJsonBody(request);
      const wantedNamespace = effectiveNamespace(body.namespace);
      const target = String(body.path || '').trim();
      if (!target) return send(response, 400, { error: 'PATH_REQUIRED', namespace: wantedNamespace });
      // A relative path would resolve against the sidecar's working directory,
      // which the caller does not control and a service definition can change.
      // Refusing is clearer than resolving to a file nobody asked for.
      if (!path.isAbsolute(target)) {
        return send(response, 400, {
          error: 'PATH_MUST_BE_ABSOLUTE',
          path: target,
          namespace: wantedNamespace,
        });
      }
      let info;
      try {
        info = fs.statSync(target);
      } catch (error) {
        return send(response, 404, {
          error: 'READ_FAILED',
          path: target,
          namespace: wantedNamespace,
          message: String((error && error.message) || error),
        });
      }
      if (!info.isFile()) {
        return send(response, 400, { error: 'NOT_A_FILE', path: target, namespace: wantedNamespace });
      }
      if (info.size > MAX_INGEST_BYTES) {
        return send(response, 413, {
          error: 'FILE_TOO_LARGE',
          path: target,
          namespace: wantedNamespace,
          bytes: info.size,
          maxBytes: MAX_INGEST_BYTES,
        });
      }
      return await serialized(async () => {
        const result = await memory.ingestFile(target, {
          namespace: wantedNamespace,
          title: body.title ? String(body.title) : undefined,
          tags: Array.isArray(body.tags) ? body.tags : [],
          source: body.source ? String(body.source) : undefined,
          pdfPassword: body.pdfPassword ? String(body.pdfPassword) : undefined,
          replace: body.replace === true,
          importance: body.importance,
        });
        if (result && result.error) {
          // ``message`` is what makes these actionable ("Tesseract not found",
          // "PDF contains no extractable text"); the status only classifies it.
          lastError = `${result.error}: ${result.message || ''}`.trim();
          const status = result.error === 'READ_FAILED' ? 404
            : result.error === 'UNSUPPORTED_DOCUMENT_TYPE' ? 415
              : 400;
          return send(response, status, {
            ...result,
            ok: false,
            namespace: wantedNamespace,
            revision: revisionOf(),
          });
        }
        return send(response, 200, {
          ...(result || {}),
          ok: true,
          namespace: wantedNamespace,
          revision: bumpRevision(),
        });
      });
    }
  } catch (error) {
    lastError = String((error && error.message) || error);
    if (error && error.code === 'BODY_TOO_LARGE') return send(response, 413, { error: 'BODY_TOO_LARGE' });
    if (error && error.code === 'BAD_JSON') return send(response, 400, { error: 'BAD_JSON' });
    // The caller hung up mid-body: there is no socket left to answer on, and
    // attempting to write would only produce a second failure.
    if (error && error.code === 'REQUEST_ABORTED') return undefined;
    return send(response, 500, { error: 'INTERNAL', message: String(error.message || error) });
  }

  // Health, remember, ingest, recall, context, metrics and documents stay the
  // engine's own routes. Writes run one at a time and bump the revision *after*
  // the engine has committed, so a compare-and-swap issued after them sees a
  // token that has moved and an export issued before them sees the old pair.
  // Bumping first would publish a revision whose contents do not exist yet.
  const readOnly = request.method === 'GET' || request.method === 'HEAD'
    || NON_MUTATING_ROUTES.has(pathname);
  if (readOnly) return engineHandler(request, response);
  return await serialized(async () => {
    const result = await engineHandler(request, response);
    bumpRevision();
    return result;
  });
});

server.on('error', (error) => {
  // A bind failure (port taken, bad host) has to be loud: the bridge waits on
  // /status and would otherwise report only a generic startup timeout.
  lastError = String((error && error.message) || error);
  process.stdout.write(`saturn-memory server error: ${lastError}\n`);
  process.exit(1);
});

server.listen(port, host, () => {
  process.stdout.write(`saturn-memory listening on http://${host}:${port} (namespace=${namespace})\n`);
});

// Last line of defence. Node's default for an unhandled rejection is to end the
// process, which the bridge then sees as "the store is gone" - every queued
// write is lost and the runtime falls back to a mode the operator did not ask
// for. Log it, keep the store serving, and let the per-request try/catch above
// be what reports failures.
process.on('unhandledRejection', (reason) => {
  try {
    const detail = reason && reason.stack ? reason.stack : String(reason);
    lastError = detail;
    process.stdout.write(`saturn-memory unhandled rejection: ${detail}\n`);
  } catch {
    /* a logging failure must not become the crash */
  }
});

function shutdown() {
  server.close(() => {
    try { memory.close(); } catch { /* ignore */ }
    process.exit(0);
  });
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
