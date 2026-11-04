// universal-memory v2: standalone assistant-grade memory.
//
// One durable JSONL store with:
//   - adaptive salience: base importance + access reinforcement + confidence
//   - type-aware recency decay (turns fade fast, preferences barely fade)
//   - dedupe/merge, contradiction versioning, TTL, namespaces, sessions
//   - hybrid retrieval (BM25 + local embeddings), query cache, optional reranker
//   - context assembly with MMR and a token budget
//   - consolidation of old episodes, purge/export/import
//   - fsync writes, compaction locking, optional checksums, pluggable storage
//
// Records:
//   {"t":"add","id","at","kind","text","tags","src",...metadata}
//   {"t":"imp","id","importance"}
//   {"t":"meta","id","fields"}
//   {"t":"vec","id","vec","model"}
//   {"t":"del","id","at"}
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { extractPdfText } = require('./pdf');
const { tesseractOcr, imageMime, popplerRenderer, popplerTextExtractor } = require('./ocr');
const { extractOfficeText } = require('./office');

const DAY = 864e5;
const DEFAULT_FILE = 'memory.jsonl';
const DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434';
const DEFAULT_MODEL = 'nomic-embed-text';
const DEFAULT_WEIGHTS = Object.freeze({ relevance: 0.6, recency: 0.25, importance: 0.15 });
const DEFAULT_DECAY_DAYS = Object.freeze({ turn: 7, episode: 30, note: 30, fact: 365, preference: 3650, summary: 365, document: 365, default: 30 });
const DEFAULT_IMPORTANCE = 0.5;
const DEFAULT_CONFIDENCE = 0.5;
const DEFAULT_TIMEOUT_MS = 20000;
const DEFAULT_BATCH_SIZE = 16;
const DEFAULT_MIN_SIMILARITY = 0.3;
const DEFAULT_DEDUPE_SIMILARITY = 0.92;
const DEFAULT_QUERY_CACHE_SIZE = 100;
const DEFAULT_ANN_THRESHOLD = 20000;
const DEFAULT_ANN_NPROBE = 8;
const DEFAULT_ANN_REBUILD = 1000;
const DEFAULT_MAX_TURNS = 2000;
const DEFAULT_COMPACT_BYTES = 5 * 1024 * 1024;
const DEFAULT_LOCK_STALE_MS = 30000;
const DEFAULT_LOCK_WAIT_MS = 1500;
const DEFAULT_LOCK_RETRY_MS = 10;
const RRF_K = 60;
const RRF_LIMIT = 50;
const MAX_SCAN = 4000;

const KINDS = Object.freeze(['note', 'turn', 'episode', 'fact', 'preference', 'summary', 'document']);
const KIND_SET = new Set(KINDS);
const RECALL_KINDS = Object.freeze(['note', 'fact', 'preference', 'episode', 'summary', 'document']);
const DEDUPE_KINDS = Object.freeze(['note', 'fact', 'preference']);
const MAX_TEXT = Object.freeze({ note: 1000, turn: 400, episode: 1000, fact: 1000, preference: 1000, summary: 2000, document: 2000 });

const STOP = new Set(('a an and are as at be but by for from has have i in is it its me my of on or so that the their then ' +
  'there these they this to was we were what when which who will with you your about did do does how not').split(' '));
const QUERY_STOP = new Set(['note', 'notes', 'journal', 'memory', 'remember', 'recall', 'said', 'say', 'write', 'wrote', 'written',
  'log', 'logged', 'tell', 'find', 'search', 'show', 'anything', 'something']);

const DEFAULT_ALIAS = Object.freeze({
  nvidia: 'nvda', apple: 'aapl', tesla: 'tsla', microsoft: 'msft', amazon: 'amzn', google: 'googl', alphabet: 'googl',
  facebook: 'meta', netflix: 'nflx', palantir: 'pltr', coinbase: 'coin', intel: 'intc', broadcom: 'avgo', disney: 'dis', boeing: 'ba',
});

// ------------------------------------------------------------------ helpers

function stem(word) {
  if (word.length > 5 && word.endsWith('ing')) return word.slice(0, -3);
  if (word.length > 4 && word.endsWith('ies')) return word.slice(0, -3) + 'y';
  if (word.length > 4 && word.endsWith('ed')) return word.slice(0, -2);
  if (word.length > 3 && word.endsWith('es')) return word.slice(0, -2);
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss')) return word.slice(0, -1);
  return word;
}

function clamp01(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0;
}

function clampImportance(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : fallback;
}

function positiveNumber(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function positiveInteger(value, fallback) {
  const n = Math.floor(Number(value));
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function normalizeWeights(input) {
  const source = input && typeof input === 'object' ? input : {};
  const out = {};
  for (const key of ['relevance', 'recency', 'importance']) {
    const n = Number(source[key]);
    out[key] = Number.isFinite(n) && n >= 0 ? n : DEFAULT_WEIGHTS[key];
  }
  return Object.freeze(out);
}

function normalizeDecay(input) {
  if (typeof input === 'number') {
    const value = positiveNumber(input, DEFAULT_DECAY_DAYS.default);
    const out = {};
    for (const kind of KINDS) out[kind] = value;
    out.default = value;
    return Object.freeze(out);
  }
  const source = input && typeof input === 'object' ? input : {};
  const out = {};
  for (const kind of [...KINDS, 'default']) {
    out[kind] = positiveNumber(source[kind], DEFAULT_DECAY_DAYS[kind]);
  }
  return Object.freeze(out);
}

function normalizeAliases(input) {
  const out = {};
  if (!input || typeof input !== 'object') return out;
  for (const [key, value] of Object.entries(input)) {
    if (value === undefined || value === null) continue;
    const token = String(key).toLowerCase().trim();
    const canonical = String(value).toLowerCase().trim();
    if (token && canonical) out[token] = canonical;
  }
  return out;
}

function normalizeHeaders(headers, apiKey) {
  const out = {};
  if (headers && typeof headers === 'object') {
    for (const [key, value] of Object.entries(headers)) {
      if (value !== undefined && value !== null) out[String(key)] = String(value);
    }
  }
  if (apiKey && !Object.keys(out).some((key) => key.toLowerCase() === 'authorization')) {
    out.Authorization = `Bearer ${String(apiKey)}`;
  }
  return Object.freeze(out);
}

function normalizeUrl(input) {
  const value = String(input === undefined ? DEFAULT_OLLAMA_URL : (input || '')).trim();
  if (!value) return '';
  return (/^https?:\/\//i.test(value) ? value : `http://${value}`).replace(/\/+$/, '');
}

function normalizeNamespace(value) {
  const text = String(value == null ? 'default' : value).trim();
  return text || 'default';
}

function normalizeKeyText(value) {
  return String(value == null ? '' : value).toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function parseTime(value) {
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function isVector(value) {
  if (!(Array.isArray(value) || ArrayBuffer.isView(value)) || !value.length) return false;
  for (let i = 0; i < value.length; i++) if (!Number.isFinite(value[i])) return false;
  return true;
}

function validVectors(value, count) {
  if (!Array.isArray(value) || value.length !== count) return false;
  return value.every(isVector);
}

function encodeVector(value) {
  return Buffer.from(new Float32Array(value).buffer).toString('base64');
}

function decodeVector(text) {
  const buffer = Buffer.from(String(text), 'base64');
  return new Float32Array(buffer.buffer, buffer.byteOffset, buffer.length / 4);
}

function normalizeVector(value) {
  let norm = 0;
  for (const x of value) norm += x * x;
  norm = Math.sqrt(norm) || 1;
  return Float32Array.from(value, (x) => x / norm);
}

function dot(a, b) {
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += a[i] * b[i];
  return sum;
}

function jaccard(a, b) {
  if (!a.size || !b.size) return 0;
  let shared = 0;
  for (const token of a) if (b.has(token)) shared++;
  return shared / (a.size + b.size - shared);
}

function recordChecksum(record) {
  const copy = { ...record };
  delete copy.chk;
  return crypto.createHash('sha256').update(JSON.stringify(copy)).digest('hex').slice(0, 16);
}

function sleepMs(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    const end = Date.now() + ms;
    while (Date.now() < end) { /* last-resort wait */ }
  }
}

class JsonlStorage {
  constructor(file, { fsync = true, checksums = false } = {}) {
    this.file = path.resolve(file);
    this.fsync = fsync !== false;
    this.checksums = checksums === true;
  }

  load() {
    let text;
    try { text = fs.readFileSync(this.file, 'utf8'); } catch { return []; }
    const records = [];
    for (const line of text.replace(/^\uFEFF/, '').split('\n')) {
      if (!line.trim()) continue;
      let record;
      try { record = JSON.parse(line); } catch { continue; }
      if (!record || typeof record !== 'object') continue;
      if (record.chk && record.chk !== recordChecksum(record)) continue;
      records.push(record);
    }
    return records;
  }

  append(record) {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const payload = { ...record };
    if (this.checksums) payload.chk = recordChecksum(payload);
    const line = JSON.stringify(payload) + '\n';
    const fd = fs.openSync(this.file, 'a');
    try {
      fs.writeFileSync(fd, line);
      if (this.fsync) fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  }

  rewrite(records) {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const lines = records.map((record) => {
      const payload = { ...record };
      if (this.checksums) payload.chk = recordChecksum(payload);
      return JSON.stringify(payload);
    });
    const tmp = `${this.file}.tmp`;
    const fd = fs.openSync(tmp, 'w');
    try {
      fs.writeFileSync(fd, lines.join('\n') + (lines.length ? '\n' : ''));
      if (this.fsync) fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, this.file);
  }

  size() {
    try { return fs.statSync(this.file).size; } catch { return 0; }
  }

  close() { /* nothing to close */ }
}

// Optional transactional backend built on Node's own SQLite module (Node 22.5+).
// The constructor loads node:sqlite lazily so older Node versions can still use JSONL.
class SqliteStorage {  constructor(file, { table = 'memory_records' } = {}) {
    const name = String(table);
    if (!/^[a-z_][a-z0-9_]*$/i.test(name)) throw new TypeError('invalid SQLite table name');
    let DatabaseSync;
    try { ({ DatabaseSync } = require('node:sqlite')); } catch {
      throw new Error('SqliteStorage requires Node.js 22.5+ (node:sqlite)');
    }
    this.table = name;
    this.file = path.resolve(file);
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    this.db = new DatabaseSync(this.file);
    this.db.exec('PRAGMA journal_mode = WAL');
    this.db.exec('PRAGMA synchronous = FULL');
    this.db.exec(`CREATE TABLE IF NOT EXISTS ${this.table} (seq INTEGER PRIMARY KEY AUTOINCREMENT, json TEXT NOT NULL)`);
  }

  load() {
    const rows = this.db.prepare(`SELECT json FROM ${this.table} ORDER BY seq`).all();
    const records = [];
    for (const row of rows) {
      try { records.push(JSON.parse(row.json)); } catch { /* skip */ }
    }
    return records;
  }

  append(record) {
    this.db.prepare(`INSERT INTO ${this.table} (json) VALUES (?)`).run(JSON.stringify(record));
  }

  rewrite(records) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`DELETE FROM ${this.table}`).run();
      const insert = this.db.prepare(`INSERT INTO ${this.table} (json) VALUES (?)`);
      for (const record of records) insert.run(JSON.stringify(record));
      this.db.exec('COMMIT');
    } catch (error) {
      try { this.db.exec('ROLLBACK'); } catch { /* ignore */ }
      throw error;
    }
  }

  size() {
    try { return fs.statSync(this.file).size; } catch { return 0; }
  }

  close() {
    if (this.db) { this.db.close(); this.db = null; }
  }
}

function slugToken(value, fallback = 'value') {
  const token = normalizeKeyText(value).split(' ').slice(0, 3).join('_');
  return token || fallback;
}

function trimValue(value) {
  return String(value || '').replace(/^[\s"'“”]+|[\s.,;:!?"'“”]+$/g, '').trim();
}

const NUMBER_WORDS = new Set(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
  'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty',
  'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety', 'hundred', 'thousand']);

function numberTokens(text) {
  const tokens = String(text || '').toLowerCase().match(/\b(?:\d+(?:\.\d+)?|v\d+|q[1-4]|[a-z]*\d+[a-z0-9]*)\b/g) || [];
  return new Set(tokens.concat((String(text || '').toLowerCase().match(/\b[a-z]+\b/g) || []).filter((token) => NUMBER_WORDS.has(token))));
}

function decodeEntities(text) {
  return String(text || '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCharCode(parseInt(code, 16)));
}

// A run of blank lines collapses to exactly one. Both the character class and
// the replacement are built from char codes so this module never depends on
// how an editor or patch tool re-encodes backslash escapes.
const LF = String.fromCharCode(10);
const BLANK_LINES = new RegExp(
  LF + '[' + String.fromCharCode(9, 10, 11, 12, 13, 32) + ']*' + LF + '+',
  'g'
);

function htmlToText(html) {
  return decodeEntities(String(html || '')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<noscript\b[^>]*>[\s\S]*?<\/noscript>/gi, ' ')
    .replace(/<(?:br|hr)\s*\/?>/gi, '\n')
    .replace(/<\/(?:p|div|section|article|li|tr|h[1-6]|blockquote|pre)>/gi, '\n')
    .replace(/<[^>]+>/g, ' '))
    .replace(/[ \t]+/g, ' ')
    .replace(BLANK_LINES, LF + LF)
    .trim();
}

function extractHtmlTitle(html) {
  const match = String(html || '').match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return match ? decodeEntities(match[1].replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim().slice(0, 160) : '';
}

function redactPII(text) {
  return String(text || '')
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, '[EMAIL]')
    .replace(/\b\d{3}-\d{2}-\d{4}\b/g, '[SSN]')
    .replace(/\b(?:\d[ -]*?){13,19}\b/g, '[CARD]')
    .replace(/\b(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b/g, '[PHONE]');
}

function escapeXmlText(text) {
  return String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function chunkText(text, { size = 1200, overlap = 200 } = {}) {
  const clean = String(text || '').replace(/\r/g, '').replace(/[ \t]+$/gm, '').replace(/\n{3,}/g, '\n\n').trim();
  if (!clean) return [];
  const limit = positiveInteger(size, 1200);
  const cover = Math.min(limit - 1, Math.max(0, positiveInteger(overlap, 200)));
  const paragraphs = clean.split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
  const units = paragraphs.flatMap((part) => (part.length <= limit ? [part] : part.split(/(?<=[.!?])\s+/)));
  const chunks = [];
  let current = '';
  const flush = () => {
    const value = current.trim();
    if (value) chunks.push(value);
    current = value.length > cover ? value.slice(-cover).replace(/^\S*\s/, '') : '';
  };
  for (const unit of units) {
    if ((current + ' ' + unit).trim().length > limit && current.trim()) flush();
    current = current ? `${current}\n${unit}` : unit;
    while (current.length > limit * 1.5) {
      chunks.push(current.slice(0, limit).trim());
      current = current.slice(limit - cover);
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

function hasNegation(text) {
  return /\b(?:not|no|never|without|none|cannot|can't|don't|doesn't|didn't|isn't|aren't|won't)\b/i.test(String(text || ''));
}

// A high cosine can still hide a changed number, date, or negation. Never merge those.
function materiallyDifferent(a, b) {
  if (hasNegation(a) !== hasNegation(b)) return true;
  const left = numberTokens(a);
  const right = numberTokens(b);
  if (left.size !== right.size) return true;
  for (const token of left) if (!right.has(token)) return true;
  return false;
}

// Offline extractor for common durable statements. This keeps ingest() useful with
// no generation model; a custom extractor or extractModel takes precedence.
function heuristicExtract(text) {
  const output = [];
  const sentences = String(text || '')
    .split(/(?<=[.!?])\s+|\n+/)
    .flatMap((sentence) => sentence.split(/\s+(?:and|but)\s+(?=(?:i|we|my|i'm|work|live|use|prefer|like|love)\b)/i))
    .map((sentence) => sentence.trim())
    .filter(Boolean);
  for (const raw of sentences) {
    const sentence = raw.replace(/^[-*]\s*/, '').trim();
    let match;
    if ((match = sentence.match(/^(?:i|we)\s+(?:really\s+)?(?:prefer|like|love|hate|dislike)\s+(.+)/i))) {
      const object = trimValue(match[1]);
      if (!object) continue;
      let predicate = `preference.${slugToken(object)}`;
      let value = object;
      const theme = object.match(/\b(dark|light)\s+(?:mode|theme)\b/i);
      const language = object.match(/\b(python|javascript|typescript|rust|golang|java)\b/i);
      const diet = object.match(/\b(vegetarian|vegan|omnivore|pescatarian)\b/i);
      if (theme) { predicate = 'theme'; value = theme[1].toLowerCase(); }
      else if (language) { predicate = 'language'; value = language[1].toLowerCase(); }
      else if (diet) { predicate = 'diet'; value = diet[1].toLowerCase(); }
      output.push({ kind: 'preference', text: `User prefers ${object}`, subject: 'user', predicate, value, confidence: 0.75, importance: 0.8 });
      continue;
    }
    if ((match = sentence.match(/^my favorite\s+([a-z ]+?)\s+is\s+(.+)/i))) {
      const key = trimValue(match[1]);
      const value = trimValue(match[2]);
      if (key && value) output.push({ kind: 'preference', text: `User's favorite ${key} is ${value}`, subject: 'user', predicate: `favorite.${slugToken(key)}`, value, confidence: 0.75, importance: 0.8 });
      continue;
    }
    if ((match = sentence.match(/^my name is\s+(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'fact', text: `User's name is ${value}`, subject: 'user', predicate: 'name', value, confidence: 0.8, importance: 0.8 });
      continue;
    }
    if ((match = sentence.match(/^(?:i|we)\s+(?:live|am living|moved|relocated)\s+(?:in|to)\s+(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'fact', text: `User lives in ${value}`, subject: 'user', predicate: 'location', value, confidence: 0.75, importance: 0.7 });
      continue;
    }
    if ((match = sentence.match(/^(?:i|we)\s+(?:work|am working)\s+(?:at|for)\s+(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'fact', text: `User works at ${value}`, subject: 'user', predicate: 'employer', value, confidence: 0.75, importance: 0.7 });
      continue;
    }
    if ((match = sentence.match(/^i(?:'m| am)\s+allergic\s+to\s+(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'fact', text: `User is allergic to ${value}`, subject: 'user', predicate: 'allergy', value, confidence: 0.8, importance: 0.85 });
      continue;
    }
    if ((match = sentence.match(/^remember\s+(?:that\s+)?(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'fact', text: value, confidence: 0.6, importance: 0.6 });
      continue;
    }
    if ((match = sentence.match(/^(?:i|we)\s+use\s+(.+)/i))) {
      const value = trimValue(match[1]);
      if (value) output.push({ kind: 'preference', text: `User uses ${value}`, subject: 'user', predicate: 'tool', value, confidence: 0.6, importance: 0.55 });
    }
  }
  return output;
}

function seededRandom(seed) {  let state = (Number(seed) || 1) >>> 0;
  return () => {
    state += 0x6D2B79F5;
    let value = Math.imul(state ^ (state >>> 15), 1 | state);
    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

// Deterministic IVF index for approximate cosine search. It is built lazily above
// `annThreshold`, keeps exact search for smaller scopes, and rebuilds periodically.
class IvfIndex {
  constructor(rows, { clusters = 0, iterations = 8, seed = 1, rebuildAfter = 1000 } = {}) {
    this.rows = new Map();
    for (const row of rows) this.rows.set(row.id, row.vec);
    this.assignment = new Map();
    this.buckets = [];
    this.centroids = [];
    this._added = 0;
    this._options = { clusters, iterations, seed, rebuildAfter };
    this._build();
  }

  get size() { return this.rows.size; }

  _build() {
    const ids = [...this.rows.keys()];
    const n = ids.length;
    this.assignment = new Map();
    if (!n) { this.centroids = []; this.buckets = []; return; }
    const dim = this.rows.get(ids[0]).length;
    const desired = this._options.clusters || Math.max(1, Math.floor(Math.sqrt(n)));
    const k = Math.max(1, Math.min(desired, n));
    const random = seededRandom(this._options.seed + this._added);
    const centroids = [Float32Array.from(this.rows.get(ids[Math.floor(random() * n)]))];

    while (centroids.length < k) {
      const distances = ids.map((id) => {
        const vector = this.rows.get(id);
        let best = -Infinity;
        for (const centroid of centroids) best = Math.max(best, dot(vector, centroid));
        return Math.max(0, 1 - best);
      });
      const total = distances.reduce((sum, value) => sum + value, 0);
      let pick = Math.floor(random() * n);
      if (total > 0) {
        let target = random() * total;
        for (let i = 0; i < distances.length; i++) {
          target -= distances[i];
          if (target <= 0) { pick = i; break; }
        }
      }
      centroids.push(Float32Array.from(this.rows.get(ids[pick])));
    }

    for (let iteration = 0; iteration < this._options.iterations; iteration++) {
      const sums = Array.from({ length: k }, () => new Float32Array(dim));
      const counts = new Array(k).fill(0);
      for (const id of ids) {
        const vector = this.rows.get(id);
        let bestIndex = 0;
        let bestScore = -Infinity;
        for (let c = 0; c < centroids.length; c++) {
          const score = dot(vector, centroids[c]);
          if (score > bestScore) { bestScore = score; bestIndex = c; }
        }
        counts[bestIndex]++;
        const sum = sums[bestIndex];
        for (let d = 0; d < dim; d++) sum[d] += vector[d];
      }
      for (let c = 0; c < centroids.length; c++) {
        if (counts[c] > 0) centroids[c] = normalizeVector(sums[c]);
      }
    }

    this.centroids = centroids;
    this.buckets = Array.from({ length: centroids.length }, () => new Set());
    for (const id of ids) {
      const index = this._nearest(this.rows.get(id));
      this.assignment.set(id, index);
      this.buckets[index].add(id);
    }
    this._added = 0;
  }

  _nearest(vector) {
    let bestIndex = 0;
    let bestScore = -Infinity;
    for (let i = 0; i < this.centroids.length; i++) {
      const score = dot(vector, this.centroids[i]);
      if (score > bestScore) { bestScore = score; bestIndex = i; }
    }
    return bestIndex;
  }

  add(id, vector) {
    if (!this.centroids.length) {
      this.rows.set(id, vector);
      this._build();
      return;
    }
    this.rows.set(id, vector);
    const previous = this.assignment.get(id);
    if (previous !== undefined && this.buckets[previous]) this.buckets[previous].delete(id);
    const index = this._nearest(vector);
    this.assignment.set(id, index);
    this.buckets[index].add(id);
    this._added++;
    if (this._options.rebuildAfter > 0 && this._added >= this._options.rebuildAfter) this._build();
  }

  remove(id) {
    const index = this.assignment.get(id);
    if (index !== undefined && this.buckets[index]) this.buckets[index].delete(id);
    this.assignment.delete(id);
    this.rows.delete(id);
  }

  serialize() {
    return {
      v: 1,
      size: this.size,
      dim: this.centroids.length ? this.centroids[0].length : 0,
      centroids: this.centroids.map((centroid) => Array.from(centroid)),
      buckets: this.buckets.map((bucket) => [...bucket]),
      assignment: [...this.assignment.entries()],
      options: this._options,
      added: this._added,
    };
  }

  static restore(data, rows, options = {}) {
    if (!data || data.v !== 1 || !Array.isArray(data.centroids) || !data.centroids.length) return null;
    const map = new Map();
    for (const row of rows) map.set(row.id, row.vec);
    if (Number(data.size) !== map.size) return null;
    const dim = Number(data.dim);
    if (!dim) return null;
    for (const vector of map.values()) if (vector.length !== dim) return null;
    const index = Object.create(IvfIndex.prototype);
    index.rows = map;
    index.centroids = data.centroids.map((centroid) => Float32Array.from(centroid));
    index.assignment = new Map();
    index.buckets = index.centroids.map(() => new Set());
    index._added = 0;
    index._options = { ...options, ...(data.options || {}) };
    for (const [id, bucket] of (data.assignment || [])) {
      if (!map.has(id) || !index.buckets[bucket]) continue;
      index.assignment.set(id, bucket);
      index.buckets[bucket].add(id);
    }
    for (const id of map.keys()) {
      if (index.assignment.has(id)) continue;
      const bucket = index._nearest(map.get(id));
      index.assignment.set(id, bucket);
      index.buckets[bucket].add(id);
    }
    return index;
  }

  search(query, { nprobe = 8, k = 200 } = {}) {
    if (!this.centroids.length) return [];
    const probes = this.centroids
      .map((centroid, index) => [index, dot(query, centroid)])
      .sort((a, b) => b[1] - a[1])
      .slice(0, Math.max(1, Math.min(nprobe, this.centroids.length)));
    const scored = [];
    for (const [index] of probes) {
      for (const id of this.buckets[index]) {
        const vector = this.rows.get(id);
        if (vector) scored.push([id, dot(query, vector)]);
      }
    }
    scored.sort((a, b) => b[1] - a[1]);
    return scored.slice(0, k);
  }
}

// AES-256-GCM encrypted JSONL. The file starts with a plaintext header carrying a
// random salt; records are encrypted individually with a derived key.
class EncryptedJsonlStorage {
  constructor(file, { key, fsync = true } = {}) {
    if (!key) throw new TypeError('EncryptedJsonlStorage requires a key or passphrase');
    this.file = path.resolve(file);
    this.fsync = fsync !== false;
    this.keyInput = key;
    this._key = null;
    this._salt = null;
  }

  _derive(salt) {
    if (Buffer.isBuffer(this.keyInput) || ArrayBuffer.isView(this.keyInput)) {
      const key = Buffer.from(this.keyInput);
      if (key.length !== 32) throw new TypeError('encryption key buffers must be 32 bytes');
      return key;
    }
    return crypto.scryptSync(String(this.keyInput), salt, 32);
  }

  _readHeader() {
    let text;
    try { text = fs.readFileSync(this.file, 'utf8'); } catch { return null; }
    const line = text.split('\n').find((value) => value.trim());
    if (!line) return null;
    let header;
    try { header = JSON.parse(line); } catch { header = null; }
    if (!header || header.t !== 'enc' || header.v !== 1) {
      const error = new Error('encrypted storage requires an encrypted file header');
      error.code = 'ENCRYPTED_STORAGE_REQUIRES_HEADER';
      throw error;
    }
    this._salt = Buffer.from(header.salt || '', 'base64');
    this._key = this._derive(this._salt);
    return header;
  }

  _ensureHeader() {
    if (this._key) return;
    const existing = this._readHeader();
    if (existing) return;
    this._salt = crypto.randomBytes(16);
    this._key = this._derive(this._salt);
    const header = JSON.stringify({ t: 'enc', v: 1, salt: this._salt.toString('base64') }) + '\n';
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const fd = fs.openSync(this.file, 'w');
    try {
      fs.writeFileSync(fd, header);
      if (this.fsync) fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  }

  _encrypt(record) {
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', this._key, iv);
    const data = Buffer.concat([cipher.update(JSON.stringify(record), 'utf8'), cipher.final()]);
    return Buffer.concat([iv, cipher.getAuthTag(), data]).toString('base64');
  }

  _decrypt(line) {
    try {
      const raw = Buffer.from(line, 'base64');
      const iv = raw.subarray(0, 12);
      const tag = raw.subarray(12, 28);
      const data = raw.subarray(28);
      const decipher = crypto.createDecipheriv('aes-256-gcm', this._key, iv);
      decipher.setAuthTag(tag);
      return JSON.parse(Buffer.concat([decipher.update(data), decipher.final()]).toString('utf8'));
    } catch {
      const error = new Error('unable to decrypt memory file (wrong key or corrupt data)');
      error.code = 'BAD_ENCRYPTION_KEY';
      throw error;
    }
  }

  load() {
    let text;
    try { text = fs.readFileSync(this.file, 'utf8'); } catch { return []; }
    const lines = text.split('\n').filter((line) => line.trim());
    if (!lines.length) return [];
    let header;
    try { header = JSON.parse(lines[0]); } catch { header = null; }
    if (!header || header.t !== 'enc' || header.v !== 1) {
      const error = new Error('encrypted storage requires an encrypted file header');
      error.code = 'ENCRYPTED_STORAGE_REQUIRES_HEADER';
      throw error;
    }
    this._salt = Buffer.from(header.salt || '', 'base64');
    this._key = this._derive(this._salt);
    const records = [];
    for (const line of lines.slice(1)) records.push(this._decrypt(line));
    return records;
  }

  append(record) {
    this._ensureHeader();
    const line = this._encrypt(record) + '\n';
    const fd = fs.openSync(this.file, 'a');
    try {
      fs.writeFileSync(fd, line);
      if (this.fsync) fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  }

  rewrite(records) {
    this._ensureHeader();
    const lines = [JSON.stringify({ t: 'enc', v: 1, salt: this._salt.toString('base64') })];
    for (const record of records) lines.push(this._encrypt(record));
    const tmp = `${this.file}.tmp`;
    const fd = fs.openSync(tmp, 'w');
    try {
      fs.writeFileSync(fd, lines.join('\n') + '\n');
      if (this.fsync) fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, this.file);
  }

  size() {
    try { return fs.statSync(this.file).size; } catch { return 0; }
  }

  rekey(newKey) {
    if (!newKey) throw new TypeError('rekey requires a new key or passphrase');
    const records = this.load();
    const previous = { keyInput: this.keyInput, key: this._key, salt: this._salt };
    this.keyInput = newKey;
    this._salt = crypto.randomBytes(16);
    this._key = this._derive(this._salt);
    try {
      this.rewrite(records);
    } catch (error) {
      this.keyInput = previous.keyInput;
      this._key = previous.key;
      this._salt = previous.salt;
      throw error;
    }
    return this.file;
  }

  close() { /* nothing to close */ }
}

function validateStorage(storage) {
  if (!storage || typeof storage !== 'object') throw new TypeError('storage must be an object');
  for (const method of ['load', 'append', 'rewrite', 'size']) {
    if (typeof storage[method] !== 'function') throw new TypeError(`storage.${method} must be a function`);
  }
  return storage;
}

// A 3xx answer means "ask somebody else". Neither the embedding path nor text
// generation may take that advice: the payload is the user's memory and the
// only endpoint validated as loopback is the one in this.ollamaUrl. Requests
// are sent with redirect: 'manual', and every 3xx is treated as a failure, so
// the transport provably cannot deliver anything to a location header.
function isRedirectStatus(status) {
  const code = Number(status);
  return Number.isFinite(code) && code >= 300 && code < 400;
}

// ------------------------------------------------------------------ class

class UniversalMemory {
  /**
   * @param {object|string} [options] constructor options, or a storage path
   * @param {string} [options.file="memory.jsonl"] JSONL storage path
   * @param {object} [options.storage] custom sync storage adapter: { load, append, rewrite, size }
   * @param {string} [options.namespace="default"] default namespace for writes/recalls
   * @param {string|null} [options.session=null] optional default session filter
   * @param {string} [options.ollamaUrl="http://127.0.0.1:11434"] local or remote Ollama base URL
   * @param {string} [options.apiKey] optional bearer token for a remote endpoint
   * @param {object} [options.headers] extra HTTP headers for embedding/generation requests
   * @param {object} [options.documentHeaders] headers used when fetching documents by URL
   * @param {Function|null} [options.documentParser] optional (buffer, meta) => text parser, e.g. PDF
   * @param {Function|null} [options.ocr] optional (imageBuffer, meta) => text OCR hook
   * @param {Function|null} [options.pdfRenderer] optional (pdfBuffer, meta) => image buffers for scanned PDFs
   * @param {boolean} [options.pdfLayout=false] use pdftotext -layout for column/table structure
   * @param {string} [options.pdfPassword] password for encrypted PDFs
   * @param {Function|null} [options.pdfTextExtractor] optional (buffer, meta) => text for encrypted/layout PDFs
   * @param {Function|null} [options.onEvent] audit/observability callback
   * @param {Function|null} [options.authorize] optional (action, context) => boolean ACL hook
   * @param {number} [options.documentTimeoutMs=30000] URL document fetch timeout
   * @param {string} [options.model="nomic-embed-text"] embedding model name
   * @param {Function|null} [options.embed] custom embedder; null/false forces keyword-only
   * @param {object} [options.weights] { relevance, recency, importance }
   * @param {number|object} [options.decayDays] type-aware recency decay in days
   * @param {object} [options.alias] replacement alias map
   * @param {object} [options.aliases] additive alias entries
   * @param {boolean} [options.dedupe=true] merge near-duplicates on write
   * @param {number} [options.dedupeSimilarity=0.92] cosine floor for semantic dedupe
   * @param {string} [options.contradictionPolicy="version"] version | reject | keep
   * @param {boolean} [options.reinforceOnRecall=true] update access statistics on recall
   * @param {Function|null} [options.rerank] optional async reranker({query,candidates})
   * @param {Function|null} [options.summarizer] optional async summarizer(texts, entries)
   * @param {Function|null} [options.extractor] optional async extractor(text, context) -> candidates
   * @param {string|null} [options.extractModel] Ollama generation model used by ingest()/extract()
   * @param {Function|null} [options.generate] custom async (prompt, options) -> text
   * @param {number} [options.extractTimeoutMs=120000] generation timeout
   * @param {boolean} [options.heuristicExtractor=true] offline extraction fallback
   * @param {number} [options.maxExtracted=8] extracted candidates per call
   * @param {number} [options.contextTokenBudget=2000] default context budget
   * @param {number} [options.contextK=8] default context entry count
   * @param {boolean} [options.contextMmr=true] default MMR diversity for context
   * @param {number} [options.modelContextWindow] model window used for adaptive budgets
   * @param {number} [options.contextBudgetRatio=0.5] fraction of usable window
   * @param {number} [options.contextReserveTokens=1024] reserved prompt/output tokens
   * @param {Function} [options.countTokens] optional token estimator
   * @param {boolean} [options.fsync=true] fsync appends/rewrites
   * @param {boolean} [options.writeLock=true] lock appends and compaction
   * @param {boolean} [options.deferEmbedding=false] store first, embed in the background
   * @param {number} [options.embedRetries=1] retries for deferred embedding failures
   * @param {boolean|string} [options.annPersist=false] persist/restore the IVF index to a sidecar
   * @param {boolean} [options.checksums=false] per-record checksums
   * @param {string|Buffer} [options.encryptionKey] encrypt JSONL at rest with AES-256-GCM
   * @param {number} [options.queryCacheSize=100] query embedding LRU size
   * @param {number} [options.timeoutMs=20000] Ollama request timeout
   * @param {number} [options.batchSize=16] embedding batch size
   * @param {number} [options.minSimilarity=0.3] semantic hit floor
   * @param {number} [options.defaultImportance=0.5] fallback importance
   * @param {number} [options.defaultConfidence=0.5] fallback confidence
   * @param {Function} [options.now] clock, mainly for deterministic tests
   */
  constructor(options = {}) {
    if (typeof options === 'string') options = { file: options };
    if (!options || typeof options !== 'object' || Array.isArray(options)) {
      throw new TypeError('UniversalMemory options must be an object or a storage path');
    }

    const file = options.file || options.storagePath || options.path || DEFAULT_FILE;
    if (options.storage) {
      if (options.encryptionKey) throw new TypeError('use either storage or encryptionKey, not both');
      this.storage = validateStorage(options.storage);
      this.file = this.storage.file ? path.resolve(String(this.storage.file)) : (options.file ? path.resolve(String(file)) : null);
    } else if (options.encryptionKey) {
      if (typeof file !== 'string' || !file.trim()) throw new TypeError('storage path must be a non-empty string');
      this.storage = new EncryptedJsonlStorage(file, { key: options.encryptionKey, fsync: options.fsync !== false });
      this.file = this.storage.file;
    } else {
      if (typeof file !== 'string' || !file.trim()) throw new TypeError('storage path must be a non-empty string');
      this.storage = new JsonlStorage(file, { fsync: options.fsync !== false, checksums: options.checksums === true });
      this.file = this.storage.file;
    }
    this._fileStorage = this.storage instanceof JsonlStorage || this.storage instanceof EncryptedJsonlStorage;

    this.namespace = normalizeNamespace(options.namespace);
    this.session = options.session == null ? null : String(options.session).slice(0, 80);
    this.ollamaUrl = normalizeUrl(options.ollamaUrl !== undefined
      ? options.ollamaUrl
      : (options.ollamaHost !== undefined ? options.ollamaHost : options.baseUrl));
    this.model = String(options.model || options.embedModel || options.embeddingModel || DEFAULT_MODEL);
    this.headers = normalizeHeaders(options.headers, options.apiKey);
    this.documentHeaders = normalizeHeaders(options.documentHeaders);
    this.documentParser = typeof options.documentParser === 'function' ? options.documentParser : null;
    this.ocr = typeof options.ocr === 'function' ? options.ocr : null;
    this.pdfRenderer = typeof options.pdfRenderer === 'function' ? options.pdfRenderer : null;
    this.pdfLayout = options.pdfLayout === true;
    this.pdfPassword = options.pdfPassword ? String(options.pdfPassword) : '';
    this.pdfTextExtractor = typeof options.pdfTextExtractor === 'function'
      ? options.pdfTextExtractor
      : options.pdfTextExtractor === null
        ? null
        : popplerTextExtractor({ command: options.pdftotextCommand || 'pdftotext', layout: this.pdfLayout });
    this.onEvent = typeof options.onEvent === 'function' ? options.onEvent : null;
    this.authorize = typeof options.authorize === 'function' ? options.authorize : null;
    this.weights = normalizeWeights(options.weights || {
      relevance: options.relevance,
      recency: options.recency,
      importance: options.importance,
    });
    this.decayDays = normalizeDecay(options.decayDays !== undefined ? options.decayDays : options.recencyDecayDays);
    this.defaultImportance = clampImportance(options.defaultImportance, DEFAULT_IMPORTANCE);
    this.defaultConfidence = clampImportance(options.defaultConfidence, DEFAULT_CONFIDENCE);
    this.timeoutMs = positiveInteger(options.timeoutMs, DEFAULT_TIMEOUT_MS);
    this.batchSize = positiveInteger(options.batchSize, DEFAULT_BATCH_SIZE);
    this.minSimilarity = Number.isFinite(Number(options.minSimilarity)) ? Number(options.minSimilarity) : DEFAULT_MIN_SIMILARITY;
    this.now = typeof options.now === 'function' ? options.now : () => Date.now();

    this.dedupeEnabled = options.dedupe !== false;
    this.dedupeSimilarity = Number.isFinite(Number(options.dedupeSimilarity))
      ? Number(options.dedupeSimilarity)
      : DEFAULT_DEDUPE_SIMILARITY;
    this.dedupeKinds = new Set(Array.isArray(options.dedupeKinds) ? options.dedupeKinds.map(String) : DEDUPE_KINDS);
    this.contradictionPolicy = ['version', 'reject', 'keep'].includes(options.contradictionPolicy)
      ? options.contradictionPolicy
      : 'version';
    this.reinforceOnRecall = options.reinforceOnRecall !== false;
    this.feedbackStep = positiveNumber(options.feedbackStep, 0.05);
    this.writeLock = options.writeLock !== false;
    this.rerank = typeof options.rerank === 'function' ? options.rerank : null;
    this.rerankCandidates = positiveInteger(options.rerankCandidates, 24);
    this.summarizer = typeof options.summarizer === 'function' ? options.summarizer : null;
    this.extractor = typeof options.extractor === 'function' ? options.extractor : null;
    this.extractModel = options.extractModel ? String(options.extractModel) : null;
    this.generate = typeof options.generate === 'function' ? options.generate : null;
    this.extractTimeoutMs = positiveInteger(options.extractTimeoutMs, 120000);
    this.documentTimeoutMs = positiveInteger(options.documentTimeoutMs, 30000);
    this.heuristicExtractor = options.heuristicExtractor !== false;
    this.maxExtracted = positiveInteger(options.maxExtracted, 8);
    this.queryCacheSize = positiveInteger(options.queryCacheSize, DEFAULT_QUERY_CACHE_SIZE);
    this.annThreshold = options.ann === false || options.annThreshold === 0
      ? Infinity
      : positiveInteger(options.annThreshold, DEFAULT_ANN_THRESHOLD);
    this.annNProbe = positiveInteger(options.annNProbe, DEFAULT_ANN_NPROBE);
    this.annClusters = Math.max(0, Math.floor(Number(options.annClusters) || 0));
    this.annRebuildAfter = positiveInteger(options.annRebuildAfter, DEFAULT_ANN_REBUILD);
    this._ann = null;
    this.annPath = typeof options.annPersist === 'string'
      ? path.resolve(options.annPersist)
      : options.annPersist === true && this.file
        ? `${this.file}.ann`
        : null;
    this.maxTurns = positiveInteger(options.maxTurns, DEFAULT_MAX_TURNS);
    this.modelContextWindow = positiveInteger(options.modelContextWindow || options.contextWindow, 0) || null;
    this.contextBudgetRatio = Math.min(0.9, Math.max(0.1, Number(options.contextBudgetRatio) || 0.5));
    this.contextReserveTokens = positiveInteger(options.contextReserveTokens, 1024);
    this.contextMaxTokens = Number(options.contextMaxTokens) > 0 ? Number(options.contextMaxTokens) : Infinity;
    this.contextTokenBudget = positiveInteger(options.contextTokenBudget, 2000);
    this.contextBudgetExplicit = options.contextTokenBudget !== undefined;
    this.contextK = positiveInteger(options.contextK, 8);
    this.contextKAuto = options.contextK === undefined;
    this.contextMmr = options.contextMmr !== false;
    this.compactBytes = positiveInteger(options.compactBytes, DEFAULT_COMPACT_BYTES);
    this.lockStaleMs = positiveInteger(options.lockStaleMs, DEFAULT_LOCK_STALE_MS);
    this.lockWaitMs = positiveInteger(options.lockWaitMs, DEFAULT_LOCK_WAIT_MS);
    this.lockRetryMs = positiveInteger(options.lockRetryMs, DEFAULT_LOCK_RETRY_MS);
    this.countTokens = typeof options.countTokens === 'function'
      ? options.countTokens
      : (text) => Math.ceil(String(text || '').length / 4);
    this.salience = typeof options.salience === 'function' ? options.salience : null;
    this.schemaVersion = 2;

    const providedFetch = options.fetch === undefined ? globalThis.fetch : options.fetch;
    this._fetch = typeof providedFetch === 'function' ? providedFetch : null;
    this.embed = typeof options.embed === 'function' ? options.embed : null;
    this.semantic = options.embed === null || options.embed === false
      ? false
      : (!!this.embed || (!!this.ollamaUrl && !!this._fetch));
    this.deferEmbedding = options.deferEmbedding === true;
    this.autoResumeEmbeddings = options.autoResumeEmbeddings === true;
    this.embedRetries = positiveInteger(options.embedRetries, 1);
    this._embedQueue = [];
    this._embedQueued = new Set();
    this._embedDraining = false;
    this._embedPromise = null;
    this._metrics = {
      remembers: 0, recalls: 0, contexts: 0, documents: 0,
      embedCalls: 0, embedFailures: 0, embedRetries: 0, recalledHits: 0, recalledMisses: 0,
    };
    this.createdAt = this.now();

    if (options.alias !== undefined) {
      this.alias = Object.freeze(normalizeAliases(options.alias));
    } else {
      this.alias = Object.freeze({ ...DEFAULT_ALIAS, ...normalizeAliases(options.aliases) });
    }

    this.lastMode = 'lexical';
    this._queryCache = new Map();
    this._schemaWritten = false;
    this._reload();
    if (this.autoResumeEmbeddings) this.resumeEmbeddings();
  }

  // ------------------------------------------------------------------ public API

  /**
   * Persist an entry, dedupe/version it, and embed when available.
   * @returns {Promise<{id?:string,entry?:object,embedded?:boolean,merged?:boolean,superseded?:string[],
   *   error?:string,conflicts?:string[]}>}
   */
  async remember(text, options = {}) {
    const {
      kind = 'note', tags = [], src = '', at = null, importance = this.defaultImportance,
      namespace = this.namespace, session = this.session, subject = null, predicate = null,
      value = null, confidence = this.defaultConfidence, validFrom = null, validTo = null,
      ttlDays = null, dedupe = this.dedupeEnabled, docId = null, title = null, chunk = null, meta = null,
    } = options;

    const k = KIND_SET.has(kind) ? kind : 'note';
    const raw = String(text == null ? '' : text).replace(/\r/g, '');
    const body = (k === 'document'
      ? raw.replace(/[ \t]+$/gm, '').replace(/\n{3,}/g, '\n\n').trim()
      : raw.replace(/\s+/g, ' ').trim()).slice(0, MAX_TEXT[k]);
    if (!body) return { error: 'EMPTY' };
    this._authorize('remember', { namespace: normalizeNamespace(namespace), kind: k, text: body });
    this._metrics.remembers++;

    const nowMs = this.now();
    const parsedAt = at ? parseTime(at) : null;
    const atMs = parsedAt !== null && parsedAt <= nowMs + DAY ? parsedAt : nowMs;
    const atIso = new Date(atMs).toISOString();
    const fromIso = validFrom && parseTime(validFrom) !== null ? new Date(validFrom).toISOString() : atIso;
    let expires = validTo && parseTime(validTo) !== null ? new Date(validTo).toISOString() : null;
    const ttl = ttlDays == null ? null : positiveNumber(ttlDays, 0);
    if (!expires && ttl) expires = new Date(atMs + ttl * DAY).toISOString();

    const entry = {
      id: `m_${Date.now().toString(36)}_${crypto.randomBytes(4).toString('hex')}`,
      at: atIso,
      kind: k,
      text: body,
      tags: (Array.isArray(tags) ? tags : []).map((tag) => String(tag).slice(0, 24)).slice(0, 8),
      src: String(src || '').slice(0, 32),
      namespace: normalizeNamespace(namespace),
      session: session == null ? null : String(session).slice(0, 80),
      subject: subject == null ? null : String(subject).slice(0, 80),
      predicate: predicate == null ? null : String(predicate).slice(0, 80),
      value: value == null ? null : String(value).slice(0, 200),
      confidence: clampImportance(confidence, this.defaultConfidence),
      validFrom: fromIso,
      validTo: expires,
      supersedes: null,
      supersededBy: null,
      consolidatedInto: null,
      ttlDays: ttl || null,
      docId: docId == null ? null : String(docId).slice(0, 80),
      title: title == null ? null : String(title).slice(0, 160),
      chunk: chunk == null ? null : Math.max(0, Math.floor(Number(chunk) || 0)),
      meta: meta && typeof meta === 'object' ? meta : null,
      importance: clampImportance(importance, this.defaultImportance),
      seen: 1,
      accessCount: 0,
      lastAccessedAt: null,
    };

    let conflicts = [];
    if (this._versioned(entry)) {
      conflicts = this._conflicts(entry);
      if (conflicts.length && this.contradictionPolicy === 'reject') {
        return { error: 'CONTRADICTION', conflicts: conflicts.map((item) => item.id) };
      }
      if (conflicts.length && this.contradictionPolicy === 'version') {
        entry.supersedes = conflicts[0].id;
      }
    }

    if (dedupe && this.dedupeKinds.has(k)) {
      const exact = this._findExact(entry) || (this._versioned(entry) ? this._findStructured(entry) : null);
      if (exact) {
        const merged = await this._mergeDuplicate(exact, entry);
        this._emit('remember', { id: merged.id, kind: k, namespace: entry.namespace, merged: true, embedded: merged.embedded });
        return merged;
      }
    }

    this._append(this._entryRecord(entry));
    if (entry.importance !== this.defaultImportance) this._append({ t: 'imp', id: entry.id, importance: entry.importance });
    this._docs.set(entry.id, entry);
    this._seq.set(entry.id, ++this._seqN);
    this._index(entry);

    let embedded = false;
    let vector = null;
    if (this.semantic && !this.deferEmbedding) {
      const vectors = await this._embedAll([entry.text]);
      if (vectors && vectors[0] && this._docs.has(entry.id)) {
        vector = vectors[0];
        this._append({ t: 'vec', id: entry.id, vec: encodeVector(vector), model: this.model });
        this._setVector(entry.id, vector, this.model);
        embedded = true;
      }
    } else if (this.semantic && this.deferEmbedding) {
      this._queueEmbedding(entry.id);
    }

    if (dedupe && vector && this.dedupeKinds.has(k)) {
      const duplicate = this._findSemanticDuplicate(entry, vector);
      if (duplicate) {
        this._append({ t: 'del', id: entry.id, at: new Date(this.now()).toISOString() });
        this._gone.add(entry.id);
        this._docs.delete(entry.id);
        this._vectors.delete(entry.id);
        this._unindex(entry.id);
        this._dead++;
        const merged = this._merge(duplicate, entry, vector);
        this._emit('remember', { id: duplicate.id, kind: k, namespace: entry.namespace, merged: true, embedded: true });
        return { id: duplicate.id, entry: merged, embedded: true, merged: true };
      }
    }

    if (conflicts.length && this.contradictionPolicy === 'version') {
      const nowIso = new Date(this.now()).toISOString();
      for (const old of conflicts) {
        this._updateEntry(old, { validTo: nowIso, supersededBy: entry.id }, false);
        this._append({ t: 'meta', id: old.id, fields: { validTo: nowIso, supersededBy: entry.id } });
      }
    }

    this._compactIfNeeded();
    this._emit('remember', { id: entry.id, kind: k, namespace: entry.namespace, embedded, pending: this.semantic && this.deferEmbedding, superseded: conflicts.map((item) => item.id) });
    return { id: entry.id, entry, embedded, pending: this.semantic && this.deferEmbedding, superseded: conflicts.map((item) => item.id) };
  }

  /**
   * Heuristic memory-kind routing for callers that want query-aware scopes.
   * @returns {string[]} candidate kinds
   */
  inferKinds(query) {
    const text = String(query || '').toLowerCase();
    const kinds = new Set();
    if (/\b(prefer|prefers|preference|favorite|favourite|like|likes|love|hate|dislike|usually|always|never)\b/.test(text)) kinds.add('preference');
    if (/\b(when|yesterday|today|last|before|after|happened|said|told|conversation|earlier)\b/.test(text)) { kinds.add('turn'); kinds.add('episode'); }
    if (/\b(who|what|which|where|name|is|are|does|fact)\b/.test(text)) kinds.add('fact');
    if (!kinds.size) { kinds.add('note'); kinds.add('fact'); kinds.add('preference'); }
    return [...kinds];
  }

  /**
   * Extract structured memory candidates from text without storing them.
   * Uses options.extractor / constructor extractor first, then an Ollama generation
   * model, then the offline heuristic extractor.
   * @returns {Promise<Array<object>>}
   */
  async extract(text, options = {}) {
    const source = String(text == null ? '' : text).trim();
    if (!source) return [];
    const extractor = options.extractor || this.extractor;
    let raw = null;
    if (extractor) {
      try {
        raw = this._candidateList(await extractor(source, {
          namespace: options.namespace === undefined ? this.namespace : options.namespace,
          session: options.session === undefined ? this.session : options.session,
          memory: this,
          now: this.now,
        }));
      } catch {
        raw = null;
      }
    } else if (this.extractModel || this.generate) {
      const generated = await this._generateText(this._extractionPrompt(source));
      raw = this._parseExtraction(generated);
    }
    if ((!Array.isArray(raw) || !raw.length) && this.heuristicExtractor) raw = heuristicExtract(source);

    const output = [];
    const seen = new Set();
    for (const candidate of (Array.isArray(raw) ? raw : [])) {
      const normalized = this._normalizeCandidate(candidate);
      if (!normalized) continue;
      const key = `${normalized.kind}\u0000${normalizeKeyText(normalized.text)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      output.push(normalized);
      if (output.length >= this.maxExtracted) break;
    }
    return output;
  }

  /**
   * Store a raw turn and extract durable memories from it.
   * @returns {Promise<{turn:object,extracted:string[],candidates:Array<object>,errors:Array<object>}>}
   */
  async ingest(text, options = {}) {
    const {
      kind = 'turn', tags = ['ingest'], src = 'ingest', at = null, importance = 0.3,
      namespace = this.namespace, session = this.session, extract = true, extractor = null,
    } = options;
    this._authorize('ingest', { namespace, kind });
    const turn = await this.remember(text, { kind, tags, src, at, importance, namespace, session, dedupe: false });
    if (turn.error) return { error: turn.error, turn };
    if (!extract) return { turn, extracted: [], candidates: [], errors: [] };

    const candidates = await this.extract(text, { namespace, session, extractor });
    const extracted = [];
    const errors = [];
    for (const candidate of candidates) {
      const result = await this.remember(candidate.text, {
        kind: candidate.kind,
        tags: ['extracted'],
        src: 'ingest',
        at,
        importance: candidate.importance,
        confidence: candidate.confidence,
        namespace,
        session,
        subject: candidate.subject,
        predicate: candidate.predicate,
        value: candidate.value,
      });
      if (result.error) errors.push(result); else extracted.push(result.id);
    }
    this._emit('ingest', { turn: turn.id, extracted, namespace, session });
    return { turn, extracted, candidates, errors };
  }

  /**
   * Chunk and store a document. Chunks become `document` entries with shared docId/title.
   * @returns {Promise<{docId:string,title:string,chunks:number,ids:string[],errors:Array<object>}>}
   */
  async ingestDocument(content, options = {}) {
    const {
      title = 'untitled', source = '', namespace = this.namespace, session = this.session,
      chunkSize = 1200, chunkOverlap = 200, importance = 0.5, tags = [], metadata = null,
      docId = null, replace = false, at = null, confidence = this.defaultConfidence,
    } = options;
    const body = String(content == null ? '' : content).trim();
    if (!body) return { error: 'EMPTY', docId: null, title: String(title), chunks: 0, ids: [], errors: [] };
    this._authorize('ingestDocument', { namespace, kind: 'document' });
    this._metrics.documents++;
    const id = docId ? String(docId).slice(0, 80) : `d_${Date.now().toString(36)}_${crypto.randomBytes(4).toString('hex')}`;
    if (replace) this.removeDocument(id, { namespace });
    const chunks = chunkText(body, { size: chunkSize, overlap: chunkOverlap });
    if (!chunks.length) return { error: 'EMPTY', docId: id, title: String(title), chunks: 0, ids: [], errors: [] };
    const ids = [];
    const errors = [];
    for (let index = 0; index < chunks.length; index++) {
      const result = await this.remember(chunks[index], {
        kind: 'document',
        tags: ['document', ...(Array.isArray(tags) ? tags : [])],
        src: source,
        importance,
        confidence,
        namespace,
        session,
        at,
        dedupe: false,
        docId: id,
        title: String(title || 'untitled').slice(0, 160),
        chunk: index,
        meta: metadata,
      });
      if (result.error) errors.push(result); else ids.push(result.id);
    }
    this._emit('ingestDocument', { docId: id, title, chunks: chunks.length, ids, namespace });
    return { docId: id, title: String(title || 'untitled'), chunks: chunks.length, ids, errors };
  }

  /**
   * Read and ingest a local text, Markdown, HTML, JSON, or custom-parsed file.
   * PDFs require the `documentParser` option.
   */
  async ingestFile(file, options = {}) {
    const target = path.resolve(String(file));
    let buffer;
    try { buffer = fs.readFileSync(target); } catch (error) {
      return { error: 'READ_FAILED', message: String(error.message || error), file: target };
    }
    const ext = path.extname(target).toLowerCase();
    const parsed = await this._parseDocument(buffer, {
      file: target, ext, contentType: options.contentType,
      parser: options.parser || options.documentParser,
      ocr: options.ocr,
      pdfRenderer: options.pdfRenderer,
      pdfTextExtractor: options.pdfTextExtractor,
      pdfPassword: options.pdfPassword,
      pdfLayout: options.pdfLayout,
    });
    if (parsed.error) return parsed;
    return this.ingestDocument(parsed.text, {
      ...options,
      title: options.title || parsed.title || path.basename(target),
      source: options.source || target,
    });
  }

  /**
   * Fetch and ingest a document from an HTTP(S) URL. Does not send Ollama credentials.
   */
  async ingestUrl(url, options = {}) {
    const target = String(url || '').trim();
    let parsedUrl;
    try { parsedUrl = new URL(target); } catch { return { error: 'BAD_URL', url: target }; }
    if (!/^https?:$/.test(parsedUrl.protocol)) return { error: 'BAD_URL', url: target };
    if (typeof this._fetch !== 'function') return { error: 'NO_FETCH', url: target };

    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), this.documentTimeoutMs) : null;
    try {
      const response = await this._fetch(target, {
        headers: { ...this.documentHeaders, ...(options.headers || {}) },
        signal: controller ? controller.signal : undefined,
      });
      if (!response || !response.ok) return { error: 'FETCH_FAILED', status: response ? response.status : null, url: target };
      const buffer = Buffer.from(await response.arrayBuffer());
      const contentType = response.headers && typeof response.headers.get === 'function'
        ? response.headers.get('content-type') || ''
        : '';
      const parsed = await this._parseDocument(buffer, {
        url: target, ext: '', contentType,
        parser: options.parser || options.documentParser,
        ocr: options.ocr,
        pdfRenderer: options.pdfRenderer,
        pdfTextExtractor: options.pdfTextExtractor,
        pdfPassword: options.pdfPassword,
        pdfLayout: options.pdfLayout,
      });
      if (parsed.error) return parsed;
      return this.ingestDocument(parsed.text, {
        ...options,
        title: options.title || parsed.title || parsedUrl.pathname.split('/').pop() || target,
        source: options.source || target,
      });
    } catch (error) {
      return { error: 'FETCH_FAILED', message: String(error.message || error), url: target };
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  documents(namespace) {
    this._authorize('read', { namespace: namespace === undefined ? null : namespace });
    const map = new Map();
    for (const entry of this._docs.values()) {
      if (namespace !== undefined && namespace !== null && entry.namespace !== namespace) continue;
      if (!entry.docId) continue;
      if (!map.has(entry.docId)) {
        map.set(entry.docId, {
          docId: entry.docId,
          title: entry.title || entry.docId,
          source: entry.src || '',
          namespace: entry.namespace,
          chunks: 0,
          at: entry.at,
          meta: entry.meta || null,
        });
      }
      const doc = map.get(entry.docId);
      doc.chunks++;
      if (entry.at < doc.at) doc.at = entry.at;
    }
    return [...map.values()].sort((a, b) => (a.at < b.at ? 1 : -1));
  }

  removeDocument(docId, { namespace } = {}) {
    const ids = [...this._docs.values()]
      .filter((entry) => entry.docId === docId && (namespace === undefined || namespace === null || entry.namespace === namespace))
      .map((entry) => entry.id);
    let deleted = 0;
    for (const id of ids) if (this.forget(id).ok) deleted++;
    return { deleted };
  }

  async _parseDocument(buffer, {
    file, url, ext, contentType = '', parser = null, ocr = null, pdfRenderer = null,
    pdfTextExtractor = null, pdfPassword = null, pdfLayout = null,
  } = {}) {
    const type = String(contentType || '').toLowerCase();
    const extension = String(ext || '').toLowerCase();
    const custom = parser || this.documentParser;
    if (custom) {
      try {
        const value = await custom(buffer, { file, url, ext: extension, contentType: type });
        if (value && typeof value === 'object') {
          return { text: String(value.text || ''), title: value.title ? String(value.title) : '' };
        }
        return { text: String(value || ''), title: '' };
      } catch (error) {
        return { error: 'PARSE_FAILED', message: String(error.message || error), file, url };
      }
    }

    const isPdf = extension === '.pdf' || type.includes('application/pdf');
    if (isPdf) {
      const native = extractPdfText(buffer);
      const extractor = pdfTextExtractor || this.pdfTextExtractor;
      const password = pdfPassword == null ? (this.pdfPassword || '') : String(pdfPassword);
      const layout = pdfLayout == null ? this.pdfLayout : pdfLayout === true;
      const encrypted = native.error === 'ENCRYPTED_PDF';
      const engine = ocr || this.ocr;
      const renderer = pdfRenderer || this.pdfRenderer;

      if (extractor && (encrypted || (layout && !native.error))) {
        try {
          const text = await extractor(buffer, { file, url, password });
          if (text && text.trim()) {
            return { text: text.trim(), title: native.title || '', pages: native.pageCount || null, encrypted };
          }
        } catch (error) {
          if (encrypted) {
            if (error.code === 'PDF_PASSWORD_REQUIRED') {
              return { error: 'PDF_PASSWORD_REQUIRED', message: String(error.message || error), file, url, encrypted: true };
            }
            if (error.code !== 'PDF_TEXT_UNAVAILABLE') {
              return { error: error.code || 'PDF_DECRYPT_FAILED', message: String(error.message || error), file, url, encrypted: true };
            }
          }
        }
      }

      if (native.error && !encrypted) return { ...native, file, url };
      if (!encrypted && native.text && native.text.replace(/\s+/g, '').length >= 10) {
        return { text: native.text, title: native.title || '', pages: native.pageCount };
      }
      if (encrypted && !engine) {
        return {
          error: 'PDF_DECRYPTION_REQUIRED',
          message: 'encrypted PDF; provide pdfPassword or pdfTextExtractor',
          file,
          url,
          encrypted: true,
        };
      }
      if (!engine) {
        return {
          error: 'OCR_REQUIRED',
          message: 'PDF contains no extractable text; provide an ocr hook for scanned pages',
          file,
          url,
          needsOcr: true,
        };
      }
      if (!renderer) {
        return {
          error: 'PDF_RENDERER_REQUIRED',
          message: 'OCR needs page images; provide pdfRenderer(buffer) returning image buffers',
          file,
          url,
          needsOcr: true,
        };
      }
      try {
        const images = await renderer(buffer, { file, url, password });
        const texts = [];
        for (const image of images || []) {
          const text = await engine(image, { contentType: 'image/png', file, url });
          if (text) texts.push(String(text).trim());
        }
        const text = texts.join('\n\n').trim();
        if (!text) return { error: 'OCR_EMPTY', message: 'OCR produced no text', file, url, needsOcr: true };
        return { text, title: native.title || '', pages: images.length, encrypted };
      } catch (error) {
        return { error: 'OCR_FAILED', message: String(error.message || error), file, url, needsOcr: true, encrypted };
      }
    }

    if (extension === '.html' || extension === '.htm' || type.includes('text/html') || type.includes('application/xhtml')) {
      const html = buffer.toString('utf8');
      return { text: htmlToText(html), title: extractHtmlTitle(html) };
    }

    const image = type.startsWith('image/') || /\.(?:png|jpe?g|webp|tiff?|bmp)$/i.test(extension);
    if (image) {
      const engine = ocr || this.ocr;
      if (!engine) {
        return { error: 'OCR_REQUIRED', message: 'Image ingestion requires an ocr hook', file, url };
      }
      try {
        const text = String(await engine(buffer, { contentType: type || 'image/png', file, url }) || '').trim();
        if (!text) return { error: 'OCR_EMPTY', message: 'OCR produced no text', file, url };
        return { text, title: '' };
      } catch (error) {
        return { error: 'OCR_FAILED', message: String(error.message || error), file, url };
      }
    }

    if (/\.(?:docx?|xlsx?|pptx?)$/i.test(extension) || type.includes('officedocument')) {
      const parsed = extractOfficeText(buffer, { ext: extension });
      if (parsed.error) return { ...parsed, file, url };
      if (!parsed.text || !parsed.text.trim()) return { error: 'OFFICE_EMPTY', message: 'no text found in Office document', file, url };
      return { text: parsed.text, title: parsed.title || '', kind: parsed.kind };
    }

    if (/\.(?:zip|rar|7z)$/i.test(extension)) {
      return { error: 'UNSUPPORTED_DOCUMENT_TYPE', message: 'provide documentParser(buffer) for this format', file, url };
    }
    return { text: buffer.toString('utf8'), title: '' };
  }

  /**
   * Hybrid recall with adaptive scoring and optional reranking.
   * @returns {Promise<Array<object>>}
   */
  async recall(query, options = {}) {
    this.lastMode = 'lexical';
    const { k = 5, kinds = RECALL_KINDS, namespace, session, includeHistory = false,
      reinforce = true, minScore = null, rerank = true, route = false, docId = null } = options;
    const text = String(query == null ? '' : query).trim();
    if (!text) return [];
    this._authorize('recall', { namespace: namespace === undefined ? this.namespace : namespace, query: text });
    this._metrics.recalls++;
    const tokens = [...new Set(this._tokenize(text).filter((token) => !QUERY_STOP.has(token)))];
    if (!tokens.length) return [];

    const items = this._select({
      kinds: route && options.kinds === undefined ? this.inferKinds(text) : kinds,
      namespace, session, includeHistory, docId,
    });
    if (!items.length) return [];

    let { ranked, mode } = await this._rank(items, text, tokens);
    if (rerank && this.rerank && ranked.length > 1) {
      ranked = await this._applyRerank(text, ranked);
      mode = mode === 'lexical' ? 'lexical' : 'hybrid';
    }
    this.lastMode = mode;
    if (!ranked.length) return [];

    const top = +ranked[0][1].toFixed(5) || 1;
    const limit = Math.max(Number(k) * 4, 12);
    const scored = [];
    for (const [id, fused] of ranked.slice(0, limit)) {
      const entry = this._docs.get(id);
      if (!entry) continue;
      const relative = +fused.toFixed(5) / top;
      const age = Math.max(0, this.now() - (parseTime(entry.at) || this.now()));
      const recency = Math.exp(-age / (this._decayFor(entry.kind) * DAY));
      const importanceScore = this._salience(entry);
      const score = this.weights.relevance * relative +
        this.weights.recency * recency +
        this.weights.importance * importanceScore;
      if (minScore !== null && score < Number(minScore)) continue;
      scored.push({
        ...entry,
        score: +score.toFixed(4),
        relevance: +relative.toFixed(4),
        recency: +recency.toFixed(4),
        importanceScore: +importanceScore.toFixed(4),
      });
    }
    scored.sort((a, b) => b.score - a.score ||
      (a.at < b.at ? 1 : a.at > b.at ? -1 : (this._seq.get(b.id) || 0) - (this._seq.get(a.id) || 0)));
    const hits = scored.slice(0, Math.max(0, Number(k) || 0));
    if (hits.length) this._metrics.recalledHits += hits.length; else this._metrics.recalledMisses++;
    if (reinforce && this.reinforceOnRecall) this._reinforce(hits);
    this._emit('recall', { query: text, k: Math.max(0, Number(k) || 0), mode: this.lastMode, ids: hits.map((hit) => hit.id) });
    return hits;
  }

  /**
   * Build a bounded, deduplicated, cited context block.
   * @returns {Promise<{text:string,entries:Array<object>,tokens:number,truncated:boolean,
   *   mode:string,citations:string[]}>}
   */
  contextBudgetFor(modelContextWindow = this.modelContextWindow) {
    const window = positiveInteger(modelContextWindow, 0);
    if (!window) return this.contextTokenBudget;
    const usable = Math.max(256, window - this.contextReserveTokens);
    return Math.max(256, Math.min(this.contextMaxTokens, Math.floor(usable * this.contextBudgetRatio)));
  }

  async context(query, options = {}) {
    const {
      tokenBudget: requestedBudget, k: requestedK, kinds = RECALL_KINDS, namespace, session,
      includeHistory = false, mmrLambda = 0.7, perSubject = 1, route = false, docId = null,
      format = 'text', redactPII: redact = false, mmr: requestedMmr, modelContextWindow, contextWindow,
    } = options;
    const window = positiveInteger(modelContextWindow || contextWindow, 0) || this.modelContextWindow;
    const tokenBudget = requestedBudget !== undefined
      ? positiveInteger(requestedBudget, this.contextTokenBudget)
      : (window && !this.contextBudgetExplicit ? this.contextBudgetFor(window) : this.contextTokenBudget);
    const autoK = window && this.contextKAuto
      ? Math.min(2000, Math.max(8, Math.ceil(tokenBudget / 10)))
      : this.contextK;
    const k = requestedK !== undefined ? positiveInteger(requestedK, this.contextK) : autoK;
    const mmr = requestedMmr !== undefined ? requestedMmr !== false : (autoK > 200 ? false : this.contextMmr);
    this._metrics.contexts++;
    const recallOptions = {
      k: Math.max(k * 4, 20), namespace, session, includeHistory, reinforce: false, route, docId,
    };
    if (options.kinds !== undefined) recallOptions.kinds = kinds;
    const candidates = await this.recall(query, recallOptions);
    if (!candidates.length) {
      return { text: '', entries: [], tokens: 0, truncated: false, mode: this.lastMode, citations: [] };
    }

    const maxScore = candidates[0].score || 1;
    const seenTexts = new Set();
    const subjectCounts = new Map();
    let pool = [];
    for (const candidate of candidates) {
      const key = normalizeKeyText(candidate.text);
      if (seenTexts.has(key)) continue;
      seenTexts.add(key);
      if (perSubject > 0 && candidate.subject && candidate.predicate) {
        const subjectKey = `${candidate.subject}|${candidate.predicate}`;
        const count = subjectCounts.get(subjectKey) || 0;
        if (count >= perSubject) continue;
      }
      pool.push(candidate);
    }

    const selected = [];
    if (!mmr) {
      selected.push(...pool.slice(0, k));
    } else {
      while (selected.length < k && pool.length) {
        let bestIndex = -1;
        let bestValue = -Infinity;
        for (let i = 0; i < pool.length; i++) {
          const candidate = pool[i];
          if (perSubject > 0 && candidate.subject && candidate.predicate) {
            const subjectKey = `${candidate.subject}|${candidate.predicate}`;
            if ((subjectCounts.get(subjectKey) || 0) >= perSubject) continue;
          }
          const relevance = (candidate.score || 0) / maxScore;
          let similarity = 0;
          if (selected.length) {
            let maxSimilarity = 0;
            for (const existing of selected) {
              maxSimilarity = Math.max(maxSimilarity, this._similarity(candidate, existing));
            }
            similarity = maxSimilarity;
          }
          const value = mmrLambda * relevance - (1 - mmrLambda) * similarity;
          if (value > bestValue) { bestValue = value; bestIndex = i; }
        }
        if (bestIndex < 0) break;
        const chosen = pool.splice(bestIndex, 1)[0];
        selected.push(chosen);
        if (chosen.subject && chosen.predicate) {
          const subjectKey = `${chosen.subject}|${chosen.predicate}`;
          subjectCounts.set(subjectKey, (subjectCounts.get(subjectKey) || 0) + 1);
        }
      }
    }

    const lines = [];
    const entries = [];
    let truncated = false;
    for (const entry of selected) {
      const meta = [entry.kind];
      if (entry.docId) meta.push(`doc:${entry.title || entry.docId}#${entry.chunk == null ? 0 : entry.chunk}`);
      if (entry.subject && entry.predicate) meta.push(`${entry.subject}.${entry.predicate}`);
      if (entry.confidence !== this.defaultConfidence) meta.push(`confidence ${entry.confidence.toFixed(2)}`);
      const entryText = redact ? redactPII(entry.text) : entry.text;
      const line = `- [${entry.id}] (${meta.join(', ')}) ${entryText}`;
      const nextText = lines.length ? `${lines.join('\n')}\n${line}` : line;
      if (this.countTokens(nextText) > tokenBudget) { truncated = true; break; }
      lines.push(line);
      entries.push({ ...entry, text: entryText, cite: `[${entry.id}]` });
    }
    const body = lines.length ? `Memory context:\n${lines.join('\n')}` : '';
    const text = body && format === 'xml' ? `<memory_context>\n${escapeXmlText(body)}\n</memory_context>` : body;
    if (entries.length && this.reinforceOnRecall) this._reinforce(entries);
    const result = {
      text,
      entries,
      tokens: this.countTokens(text),
      truncated,
      mode: this.lastMode,
      citations: entries.map((entry) => entry.cite),
    };
    this._emit('context', { query: String(query), tokens: result.tokens, ids: entries.map((entry) => entry.id), mode: result.mode });
    return result;
  }

  forget(id) {
    const entry = this._docs.get(id);
    if (!entry) return { error: 'NOT_FOUND' };
    this._authorize('forget', { namespace: entry.namespace, kind: entry.kind, id });
    this._append({ t: 'del', id, at: new Date(this.now()).toISOString() });
    this._gone.add(id);
    this._docs.delete(id);
    this._vectors.delete(id);
    if (this._ann) this._ann.remove(id);
    this._unindex(id);
    this._seq.delete(id);
    this._dead++;
    this._emit('forget', { id, namespace: entry.namespace, kind: entry.kind });
    return { ok: true };
  }

  feedback(id, { useful = true, delta = this.feedbackStep } = {}) {
    const entry = this._docs.get(id);
    if (!entry) return { error: 'NOT_FOUND' };
    this._authorize('feedback', { namespace: entry.namespace, kind: entry.kind, id });
    const step = Math.abs(Number(delta) || this.feedbackStep);
    const importance = clamp01(entry.importance + (useful ? step : -step));
    entry.importance = importance;
    this._append({ t: 'imp', id, importance });
    this._emit('feedback', { id, namespace: entry.namespace, kind: entry.kind, useful, importance });
    return { ok: true, entry };
  }

  history(subject, predicate, { namespace, kind = null } = {}) {
    const ns = namespace === undefined ? this.namespace : namespace;
    this._authorize('read', { namespace: ns });
    const subjectKey = normalizeKeyText(subject);
    const predicateKey = normalizeKeyText(predicate);
    return [...this._docs.values()]
      .filter((entry) => (ns === null || entry.namespace === ns) &&
        normalizeKeyText(entry.subject) === subjectKey &&
        normalizeKeyText(entry.predicate) === predicateKey &&
        (!kind || entry.kind === kind))
      .sort((a, b) => (a.validFrom < b.validFrom ? -1 : a.validFrom > b.validFrom ? 1 : 0));
  }

  get(id) {
    const entry = this._docs.get(id) || null;
    if (entry) this._authorize('read', { namespace: entry.namespace, kind: entry.kind, id });
    return entry;
  }

  recent(n = 5, options = {}) {
    const opts = Array.isArray(options) ? { kinds: options } : options;
    this._authorize('read', { namespace: opts.namespace === undefined ? this.namespace : opts.namespace });
    const wanted = this._kindSet(opts.kinds === undefined ? ['note', 'fact', 'preference', 'episode', 'summary'] : opts.kinds);
    const items = this._select({
      kinds: opts.kinds === undefined ? ['note', 'fact', 'preference', 'episode', 'summary'] : opts.kinds,
      namespace: opts.namespace,
      session: opts.session,
      includeHistory: opts.includeHistory === true,
    });
    return items
      .filter((entry) => !wanted || wanted.has(entry.kind))
      .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : (this._seq.get(b.id) || 0) - (this._seq.get(a.id) || 0)))
      .slice(0, n);
  }

  turns(n = 12, options = {}) {
    const opts = Array.isArray(options) ? { kinds: options } : options;
    this._authorize('read', { namespace: opts.namespace === undefined ? this.namespace : opts.namespace });
    return this._select({
      kinds: ['turn'],
      namespace: opts.namespace,
      session: opts.session,
      includeHistory: opts.includeHistory === true,
    })
      .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : (this._seq.get(a.id) || 0) - (this._seq.get(b.id) || 0)))
      .slice(-n);
  }

  count(kinds, options = {}) {
    if (kinds && typeof kinds === 'object' && !Array.isArray(kinds)) {
      options = kinds;
      kinds = options.kinds;
    }
    this._authorize('read', { namespace: options.namespace === undefined ? null : options.namespace });
    return this._select({
      kinds: kinds === undefined ? null : kinds,
      namespace: options.namespace === undefined ? null : options.namespace,
      session: options.session,
      includeHistory: options.includeHistory === true,
    }).length;
  }

  namespaces() {
    return [...new Set([...this._docs.values()].map((entry) => entry.namespace))].sort();
  }

  stats(namespace) {
    this._authorize('read', { namespace: namespace === undefined ? null : namespace });
    const entries = [...this._docs.values()].filter((entry) =>
      namespace === undefined || namespace === null || entry.namespace === namespace);
    const byKind = {};
    let embedded = 0;
    let active = 0;
    let archived = 0;
    let accesses = 0;
    for (const entry of entries) {
      byKind[entry.kind] = (byKind[entry.kind] || 0) + 1;
      if (this._active(entry)) active++; else archived++;
      accesses += entry.accessCount || 0;
      const vector = this._vectors.get(entry.id);
      if (vector && vector.model === this.model) embedded++;
    }
    let bytes = 0;
    try { bytes = this.storage.size(); } catch { /* optional */ }
    return {
      count: entries.length,
      byKind,
      bytes,
      embedded,
      pending: entries.length - embedded,
      model: this.model || null,
      active,
      archived,
      accesses,
      namespaces: this.namespaces().length,
      schemaVersion: this.schemaVersion,
      ann: this._ann ? { size: this._ann.size, clusters: this._ann.centroids.length, nprobe: this.annNProbe } : null,
      annThreshold: this.annThreshold === Infinity ? null : this.annThreshold,
    };
  }

  compact() {
    this._authorize('compact', {});
    const result = this._compactIfNeeded(true);
    this.saveAnn();
    this._emit('compact', { result });
    return result;
  }

  _queueEmbedding(id) {
    if (!this.semantic || !id || this._embedQueued.has(id)) return;
    this._embedQueued.add(id);
    this._embedQueue.push(id);
    if (!this._embedDraining) {
      this._embedDraining = true;
      this._embedPromise = this._drainEmbeddings().finally(() => {
        this._embedDraining = false;
        this._embedPromise = null;
      });
    }
  }

  async _drainEmbeddings() {
    while (this._embedQueue.length) {
      const id = this._embedQueue.shift();
      this._embedQueued.delete(id);
      const entry = this._docs.get(id);
      if (!entry) continue;
      let vector = null;
      for (let attempt = 0; attempt <= this.embedRetries; attempt++) {
        const vectors = await this._embedAll([entry.text]);
        if (vectors && vectors[0]) { vector = vectors[0]; break; }
        if (attempt < this.embedRetries) {
          this._metrics.embedRetries++;
          await new Promise((resolve) => setTimeout(resolve, 20 * (2 ** attempt)));
        }
      }
      if (!vector || !this._docs.has(id)) continue;
      this._append({ t: 'vec', id, vec: encodeVector(vector), model: this.model });
      this._setVector(id, vector, this.model);
      this._emit('embed', { id, model: this.model });
    }
  }

  async resumeEmbeddings() {
    if (!this.semantic) return { queued: 0 };
    const todo = this._unembedded();
    for (const entry of todo) this._queueEmbedding(entry.id);
    return { queued: todo.length };
  }

  pendingEmbeddings() {
    return this._embedQueued.size;
  }

  async flushEmbeddings() {
    while (this._embedPromise) await this._embedPromise;
    return { pending: this.pendingEmbeddings() };
  }

  /**
   * Operational counters for monitoring.
   */
  metrics() {
    return {
      ...this._metrics,
      pendingEmbeddings: this.pendingEmbeddings(),
      memoryCount: this._docs.size,
      uptimeMs: this.now() - this.createdAt,
      mode: this.lastMode,
    };
  }

  /**
   * Rescan storage and embed entries missing a vector or using another model.
   * @returns {Promise<{embedded:number,pending:number,error?:string}>}
   */
  async reindex() {
    this._authorize('reindex', { namespace: this.namespace });
    this._reload();
    const todo = this._unembedded();
    if (!todo.length) return { embedded: 0, pending: 0 };
    if (!this.semantic) return { embedded: 0, pending: todo.length, error: 'NO_EMBEDDER' };

    const vectors = await this._embedAll(todo.map((entry) => entry.text));
    if (!vectors) return { embedded: 0, pending: todo.length, error: 'NO_EMBEDDER' };

    let embedded = 0;
    todo.forEach((entry, index) => {
      if (!this._docs.has(entry.id) || !vectors[index]) return;
      this._append({ t: 'vec', id: entry.id, vec: encodeVector(vectors[index]), model: this.model });
      this._setVector(entry.id, vectors[index], this.model);
      embedded++;
    });
    return { embedded, pending: this._unembedded().length };
  }
  /**
   * Merge near-duplicate active entries.
   * @returns {Promise<{merged:number,dryRun:boolean}>}
   */
  async dedupe(options = {}) {
    const { kinds = [...this.dedupeKinds], namespace = this.namespace, similarity = this.dedupeSimilarity, dryRun = false, maxScan = MAX_SCAN } = options;
    this._authorize('dedupe', { namespace });
    const wanted = this._kindSet(kinds);
    const entries = [...this._docs.values()]
      .filter((entry) => this._active(entry) && (!wanted || wanted.has(entry.kind)) &&
        (namespace === null || entry.namespace === namespace))
      .sort((a, b) => (a.at < b.at ? -1 : 1))
      .slice(0, maxScan);
    let merged = 0;
    for (let i = 0; i < entries.length; i++) {
      const canonical = entries[i];
      if (!this._docs.has(canonical.id) || !this._active(canonical)) continue;
      for (let j = i + 1; j < entries.length; j++) {
        const other = entries[j];
        if (!this._docs.has(other.id) || !this._active(other)) continue;
        if (!this._duplicates(canonical, other, similarity)) continue;
        merged++;
        if (!dryRun) {
          this._merge(canonical, other, this._vectors.get(other.id) ? this._vectors.get(other.id).vec : null);
          this.forget(other.id);
        }
      }
    }
    if (!dryRun && merged) this._compactIfNeeded();
    this._emit('dedupe', { merged, namespace, dryRun });
    return { merged, dryRun };
  }

  /**
   * Consolidate old episodes/turns into summary entries.
   * @returns {Promise<{groups:number,summaries:string[],archived:number,dryRun:boolean}>}
   */
  async consolidate(options = {}) {
    const { olderThanDays = 30, kinds = ['turn', 'episode'], minGroupSize = 2, similarity = 0.85,
      summarizer = this.summarizer, namespace = this.namespace, dryRun = false, maxScan = MAX_SCAN,
      extract = false, extractor = null } = options;
    this._authorize('consolidate', { namespace });
    const cutoff = this.now() - positiveNumber(olderThanDays, 30) * DAY;
    const wanted = this._kindSet(kinds);
    const entries = [...this._docs.values()]
      .filter((entry) => this._active(entry) && (!wanted || wanted.has(entry.kind)) &&
        (namespace === null || entry.namespace === namespace) && (parseTime(entry.at) || 0) < cutoff)
      .sort((a, b) => (a.at < b.at ? -1 : 1))
      .slice(0, maxScan);

    const groups = [];
    const used = new Set();
    for (let i = 0; i < entries.length; i++) {
      if (used.has(entries[i].id)) continue;
      const group = [entries[i]];
      used.add(entries[i].id);
      for (let j = i + 1; j < entries.length; j++) {
        if (used.has(entries[j].id)) continue;
        if (group.some((member) => this._similarity(member, entries[j]) >= similarity)) {
          group.push(entries[j]);
          used.add(entries[j].id);
        }
      }
      if (group.length >= minGroupSize) groups.push(group);
    }

    if (dryRun) return { groups: groups.length, summaries: [], archived: 0, extracted: [], dryRun: true };
    const summaries = [];
    const extracted = [];
    let archived = 0;
    for (const group of groups) {
      if (extract) {
        const candidates = await this.extract(group.map((entry) => entry.text).join('\n'), {
          namespace: group[0].namespace,
          session: group[0].session,
          extractor,
        });
        for (const candidate of candidates) {
          const result = await this.remember(candidate.text, {
            kind: candidate.kind,
            tags: ['extracted', 'consolidated'],
            src: 'consolidate-extract',
            importance: candidate.importance,
            confidence: candidate.confidence,
            namespace: group[0].namespace,
            session: group[0].session,
            subject: candidate.subject,
            predicate: candidate.predicate,
            value: candidate.value,
          });
          if (result.id) extracted.push(result.id);
        }
      }
      let text;
      try {
        text = summarizer
          ? await summarizer(group.map((entry) => entry.text), group)
          : [...new Set(group.map((entry) => entry.text))].join(' | ');
      } catch {
        text = [...new Set(group.map((entry) => entry.text))].join(' | ');
      }
      const summary = await this.remember(String(text || '').slice(0, MAX_TEXT.summary), {
        kind: 'summary',
        tags: ['consolidated'],
        importance: Math.max(...group.map((entry) => entry.importance)),
        confidence: Math.min(...group.map((entry) => entry.confidence)),
        src: 'consolidate',
        namespace: group[0].namespace,
        session: group[0].session,
      });
      if (summary.error) continue;
      summaries.push(summary.id);
      const nowIso = new Date(this.now()).toISOString();
      for (const entry of group) {
        this._updateEntry(entry, { validTo: nowIso, consolidatedInto: summary.id }, false);
        this._append({ t: 'meta', id: entry.id, fields: { validTo: nowIso, consolidatedInto: summary.id } });
        archived++;
      }
    }
    if (summaries.length) this._compactIfNeeded();
    this._emit('consolidate', { groups: groups.length, summaries, archived, extracted, namespace });
    return { groups: groups.length, summaries, archived, extracted, dryRun: false };
  }

  /**
   * Hard-delete matching entries (tombstones; compaction removes them).
   * @returns {{deleted:number}}
   */
  purge(filter = {}) {
    const { namespace, kind, kinds, ids, subject, predicate, olderThanDays, expiredOnly = false } = filter;
    this._authorize('purge', { namespace });
    const wanted = this._kindSet(kinds === undefined ? (kind ? [kind] : null) : kinds);
    const idSet = ids == null ? null : new Set(Array.isArray(ids) ? ids : [ids]);
    const cutoff = olderThanDays === undefined ? null : this.now() - positiveNumber(olderThanDays, 1) * DAY;
    let deleted = 0;
    for (const entry of [...this._docs.values()]) {
      if (namespace !== undefined && namespace !== null && entry.namespace !== namespace) continue;
      if (wanted && !wanted.has(entry.kind)) continue;
      if (idSet && !idSet.has(entry.id)) continue;
      if (subject !== undefined && normalizeKeyText(entry.subject) !== normalizeKeyText(subject)) continue;
      if (predicate !== undefined && normalizeKeyText(entry.predicate) !== normalizeKeyText(predicate)) continue;
      if (cutoff !== null && (parseTime(entry.at) || 0) >= cutoff) continue;
      if (expiredOnly && this._active(entry)) continue;
      if (this.forget(entry.id).ok) deleted++;
    }
    this._emit('purge', { deleted, namespace });
    return { deleted };
  }

  export({ namespace = null, includeVectors = false, includeHistory = true } = {}) {
    this._authorize('export', { namespace });
    const entries = [...this._docs.values()]
      .filter((entry) => includeHistory || this._active(entry))
      .filter((entry) => namespace === null || namespace === undefined || entry.namespace === namespace)
      .map((entry) => {
        const copy = { ...entry, tags: [...entry.tags] };
        if (includeVectors) {
          const vector = this._vectors.get(entry.id);
          if (vector) { copy.vec = encodeVector(vector.vec); copy.vecModel = vector.model; }
        }
        return copy;
      });
    return { schemaVersion: this.schemaVersion, exportedAt: new Date(this.now()).toISOString(), entries };
  }

  async import(data, { namespace = null, preserveIds = true } = {}) {
    this._authorize('import', { namespace });
    const entries = Array.isArray(data) ? data : (data && Array.isArray(data.entries) ? data.entries : []);
    let imported = 0;
    let skipped = 0;
    for (const source of entries) {
      if (!source || typeof source.text !== 'string' || !source.text.trim()) { skipped++; continue; }
      const id = typeof source.id === 'string' ? source.id : null;
      if (preserveIds && id && this._docs.has(id)) { skipped++; continue; }
      const canPreserve = preserveIds && id && !this._gone.has(id);
      if (!canPreserve) {
        const result = await this.remember(source.text, {
          kind: source.kind, tags: source.tags, src: source.src || 'import', at: source.at,
          importance: source.importance, namespace: namespace || source.namespace, session: source.session,
          subject: source.subject, predicate: source.predicate, value: source.value,
          confidence: source.confidence, validFrom: source.validFrom, validTo: source.validTo,
          ttlDays: source.ttlDays, dedupe: false,
          docId: source.docId, title: source.title, chunk: source.chunk, meta: source.meta,
        });
        if (result.error) skipped++; else imported++;
        continue;
      }
      const entry = this._entryFromImport(source, namespace);
      this._append(this._entryRecord(entry));
      if (entry.importance !== this.defaultImportance) this._append({ t: 'imp', id: entry.id, importance: entry.importance });
      this._docs.set(entry.id, entry);
      this._seq.set(entry.id, ++this._seqN);
      this._index(entry);
      if (typeof source.vec === 'string') {
        const vector = this._readVector(source.vec);
        if (vector) {
          this._append({ t: 'vec', id: entry.id, vec: source.vec, model: source.vecModel || this.model });
          this._setVector(entry.id, vector, source.vecModel || this.model);
        }
      }
      imported++;
    }
    if (imported) this._compactIfNeeded();
    this._emit('import', { imported, skipped, namespace });
    return { imported, skipped };
  }

  clearQueryCache() {
    this._queryCache.clear();
  }

  rotateEncryptionKey(newKey) {
    if (!this.storage || typeof this.storage.rekey !== 'function') return { error: 'NOT_ENCRYPTED' };
    if (!newKey) return { error: 'KEY_REQUIRED' };
    this._authorize('rotateKey', {});
    this.storage.rekey(newKey);
    this._emit('rotateKey', { file: this.file });
    return { ok: true, file: this.file };
  }

  close() {
    this.saveAnn();
    if (typeof this.storage.close === 'function') this.storage.close();
  }

  // ------------------------------------------------------------------ internals

  _emit(type, payload = {}) {
    if (!this.onEvent) return;
    try { this.onEvent({ type, at: new Date(this.now()).toISOString(), ...payload }); } catch { /* audit must not break memory */ }
  }

  _authorize(action, context = {}) {
    if (!this.authorize) return;
    let allowed;
    try { allowed = this.authorize(action, context); } catch (error) {
      const denied = new Error('FORBIDDEN');
      denied.code = 'FORBIDDEN';
      denied.cause = error;
      throw denied;
    }
    if (!allowed) {
      const error = new Error('FORBIDDEN');
      error.code = 'FORBIDDEN';
      throw error;
    }
  }

  _reload() {
    this._docs = new Map();
    this._vectors = new Map();
    this._pendingVec = new Map();
    this._pendingImp = new Map();
    this._pendingMeta = new Map();
    this._gone = new Set();
    this._seq = new Map();
    this._seqN = 0;
    this._dead = 0;
    this._lines = 0;
    this._tf = new Map();
    this._len = new Map();
    this._tokens = new Map();
    this._df = new Map();
    this._keys = new Map();
    this._idKeys = new Map();
    this._totalLen = 0;
    this._schemaWritten = false;
    this._ann = null;

    let records = [];
    try {
      records = this.storage.load();
    } catch (error) {
      if (error && (error.code === 'BAD_ENCRYPTION_KEY' || error.code === 'ENCRYPTED_STORAGE_REQUIRES_HEADER')) throw error;
      records = [];
    }
    if (!Array.isArray(records)) throw new TypeError('storage.load() must return an array of records');
    this._lines = records.length;
    for (const record of records) {
      try { this._apply(record); } catch { /* corrupt record: skip */ }
    }
  }

  _apply(record) {
    if (!record || typeof record !== 'object') return;
    if (record.t === 'schema') {
      this._schemaWritten = true;
      this._fileSchemaVersion = Number(record.v) || 1;
      return;
    }
    if (record.t === 'add' && typeof record.id === 'string' && typeof record.text === 'string' &&
      typeof record.at === 'string' && !this._docs.has(record.id) && !this._gone.has(record.id)) {
      const entry = this._entryFromRecord(record);
      this._docs.set(entry.id, entry);
      this._seq.set(entry.id, ++this._seqN);
      this._index(entry);
      if (typeof record.vec === 'string') {
        const vector = this._readVector(record.vec);
        if (vector) this._setVector(entry.id, vector, record.model);
      }
      const pendingVec = this._pendingVec.get(entry.id);
      if (pendingVec) {
        this._pendingVec.delete(entry.id);
        const vector = this._readVector(pendingVec.vec);
        if (vector) this._setVector(entry.id, vector, pendingVec.model);
      }
      if (this._pendingImp.has(entry.id)) {
        entry.importance = this._pendingImp.get(entry.id);
        this._pendingImp.delete(entry.id);
      }
      const pendingMeta = this._pendingMeta.get(entry.id);
      if (pendingMeta) {
        this._pendingMeta.delete(entry.id);
        this._updateEntry(entry, pendingMeta, false);
      }
      return;
    }
    if (record.t === 'del' && typeof record.id === 'string') {
      this._gone.add(record.id);
      if (this._docs.has(record.id)) { this._docs.delete(record.id); this._dead++; }
      this._vectors.delete(record.id);
      if (this._ann) this._ann.remove(record.id);
      this._pendingVec.delete(record.id);
      this._pendingImp.delete(record.id);
      this._pendingMeta.delete(record.id);
      this._unindex(record.id);
      this._seq.delete(record.id);
      return;
    }
    if (record.t === 'imp' && typeof record.id === 'string') {
      const importance = clampImportance(record.importance, this.defaultImportance);
      if (this._docs.has(record.id)) this._docs.get(record.id).importance = importance;
      else if (!this._gone.has(record.id)) this._pendingImp.set(record.id, importance);
      return;
    }
    if (record.t === 'meta' && typeof record.id === 'string' && record.fields && typeof record.fields === 'object') {
      if (this._docs.has(record.id)) this._updateEntry(this._docs.get(record.id), record.fields, false);
      else if (!this._gone.has(record.id)) {
        this._pendingMeta.set(record.id, { ...(this._pendingMeta.get(record.id) || {}), ...record.fields });
      }
      return;
    }
    if (record.t === 'vec' && typeof record.id === 'string' && typeof record.vec === 'string') {
      if (this._docs.has(record.id)) {
        const vector = this._readVector(record.vec);
        if (vector) this._setVector(record.id, vector, record.model);
      } else if (!this._gone.has(record.id)) {
        this._pendingVec.set(record.id, { vec: record.vec, model: record.model });
      }
    }
  }

  _entryFromRecord(record) {
    const at = record.at;
    const ttl = record.ttlDays == null ? null : positiveNumber(record.ttlDays, 0) || null;
    let validTo = record.validTo && parseTime(record.validTo) !== null ? new Date(record.validTo).toISOString() : null;
    if (!validTo && ttl) validTo = new Date((parseTime(at) || this.now()) + ttl * DAY).toISOString();
    return {
      id: record.id,
      at,
      kind: String(record.kind || 'note'),
      text: record.text,
      tags: Array.isArray(record.tags) ? record.tags.map(String).slice(0, 8) : [],
      src: record.src ? String(record.src) : '',
      namespace: normalizeNamespace(record.namespace),
      session: record.session == null ? null : String(record.session),
      subject: record.subject == null ? null : String(record.subject),
      predicate: record.predicate == null ? null : String(record.predicate),
      value: record.value == null ? null : String(record.value),
      confidence: clampImportance(record.confidence, this.defaultConfidence),
      validFrom: record.validFrom && parseTime(record.validFrom) !== null ? new Date(record.validFrom).toISOString() : at,
      validTo,
      supersedes: record.supersedes || null,
      supersededBy: record.supersededBy || null,
      consolidatedInto: record.consolidatedInto || null,
      ttlDays: ttl,
      docId: record.docId == null ? null : String(record.docId),
      title: record.title == null ? null : String(record.title),
      chunk: record.chunk == null ? null : Math.max(0, Math.floor(Number(record.chunk) || 0)),
      meta: record.meta && typeof record.meta === 'object' ? record.meta : null,
      importance: clampImportance(record.importance, this.defaultImportance),
      seen: positiveInteger(record.seen, 1),
      accessCount: Math.max(0, Math.floor(Number(record.accessCount) || 0)),
      lastAccessedAt: record.lastAccessedAt || null,
    };
  }

  _entryFromImport(source, namespace) {
    const kind = KIND_SET.has(source.kind) ? source.kind : 'note';
    const at = source.at && parseTime(source.at) !== null ? new Date(source.at).toISOString() : new Date(this.now()).toISOString();
    const ttl = source.ttlDays == null ? null : positiveNumber(source.ttlDays, 0) || null;
    let validTo = source.validTo && parseTime(source.validTo) !== null ? new Date(source.validTo).toISOString() : null;
    if (!validTo && ttl) validTo = new Date((parseTime(at) || this.now()) + ttl * DAY).toISOString();
    return {
      id: source.id,
      at,
      kind,
      text: String(source.text).replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT[kind]),
      tags: (Array.isArray(source.tags) ? source.tags : []).map((tag) => String(tag).slice(0, 24)).slice(0, 8),
      src: String(source.src || 'import').slice(0, 32),
      namespace: normalizeNamespace(namespace || source.namespace),
      session: source.session == null ? null : String(source.session).slice(0, 80),
      subject: source.subject == null ? null : String(source.subject).slice(0, 80),
      predicate: source.predicate == null ? null : String(source.predicate).slice(0, 80),
      value: source.value == null ? null : String(source.value).slice(0, 200),
      confidence: clampImportance(source.confidence, this.defaultConfidence),
      validFrom: source.validFrom && parseTime(source.validFrom) !== null ? new Date(source.validFrom).toISOString() : at,
      validTo,
      supersedes: source.supersedes || null,
      supersededBy: source.supersededBy || null,
      consolidatedInto: source.consolidatedInto || null,
      ttlDays: ttl,
      docId: source.docId == null ? null : String(source.docId).slice(0, 80),
      title: source.title == null ? null : String(source.title).slice(0, 160),
      chunk: source.chunk == null ? null : Math.max(0, Math.floor(Number(source.chunk) || 0)),
      meta: source.meta && typeof source.meta === 'object' ? source.meta : null,
      importance: clampImportance(source.importance, this.defaultImportance),
      seen: positiveInteger(source.seen, 1),
      accessCount: Math.max(0, Math.floor(Number(source.accessCount) || 0)),
      lastAccessedAt: source.lastAccessedAt || null,
    };
  }

  _entryRecord(entry) {
    const record = {
      t: 'add', id: entry.id, at: entry.at, kind: entry.kind, text: entry.text,
      tags: entry.tags, src: entry.src,
    };
    if (entry.namespace !== 'default') record.namespace = entry.namespace;
    if (entry.docId) record.docId = entry.docId;
    if (entry.title) record.title = entry.title;
    if (entry.chunk != null) record.chunk = entry.chunk;
    if (entry.meta) record.meta = entry.meta;
    if (entry.session) record.session = entry.session;
    if (entry.subject != null) record.subject = entry.subject;
    if (entry.predicate != null) record.predicate = entry.predicate;
    if (entry.value != null) record.value = entry.value;
    if (entry.confidence !== this.defaultConfidence) record.confidence = entry.confidence;
    if (entry.validFrom && entry.validFrom !== entry.at) record.validFrom = entry.validFrom;
    if (entry.validTo) record.validTo = entry.validTo;
    if (entry.supersedes) record.supersedes = entry.supersedes;
    if (entry.supersededBy) record.supersededBy = entry.supersededBy;
    if (entry.consolidatedInto) record.consolidatedInto = entry.consolidatedInto;
    if (entry.ttlDays) record.ttlDays = entry.ttlDays;
    if (entry.seen > 1) record.seen = entry.seen;
    if (entry.accessCount > 0) record.accessCount = entry.accessCount;
    if (entry.lastAccessedAt) record.lastAccessedAt = entry.lastAccessedAt;
    return record;
  }

  _append(record) {
    if (this._fileStorage && this.file) fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const write = () => {
      if (!this._schemaWritten) {
        this.storage.append({ t: 'schema', v: this.schemaVersion });
        this._schemaWritten = true;
        this._lines++;
      }
      this.storage.append(record);
      this._lines++;
    };
    if (this.writeLock && this._fileStorage && this.file) {
      const written = this._withFileLock(`${this.file}.lock`, write);
      if (written === false) throw new Error('MEMORY_LOCK_TIMEOUT');
    } else {
      write();
    }
  }

  _readVector(text) {
    try { return decodeVector(text); } catch { return null; }
  }

  _setVector(id, vector, model) {
    if (!id || !vector || !vector.length) return;
    const tag = String(model == null ? '' : model);
    const normalized = normalizeVector(vector);
    this._vectors.set(id, { vec: normalized, model: tag });
    if (this._ann) {
      if (tag === this.model) this._ann.add(id, normalized);
      else this._ann.remove(id);
    }
  }

  _updateEntry(entry, fields, reindex = false) {
    let needsIndex = false;
    for (const key of ['text', 'tags', 'namespace', 'subject', 'predicate', 'title']) if (key in fields) needsIndex = true;
    if (needsIndex || reindex) this._unindex(entry.id);
    if (fields.text !== undefined) entry.text = String(fields.text).slice(0, MAX_TEXT[entry.kind] || 1000);
    if (fields.tags !== undefined) entry.tags = (Array.isArray(fields.tags) ? fields.tags : []).map(String).slice(0, 8);
    if (fields.at !== undefined && parseTime(fields.at) !== null) entry.at = new Date(fields.at).toISOString();
    if (fields.namespace !== undefined) entry.namespace = normalizeNamespace(fields.namespace);
    if (fields.session !== undefined) entry.session = fields.session == null ? null : String(fields.session);
    if (fields.subject !== undefined) entry.subject = fields.subject == null ? null : String(fields.subject);
    if (fields.predicate !== undefined) entry.predicate = fields.predicate == null ? null : String(fields.predicate);
    if (fields.value !== undefined) entry.value = fields.value == null ? null : String(fields.value);
    if (fields.confidence !== undefined) entry.confidence = clampImportance(fields.confidence, this.defaultConfidence);
    if (fields.validFrom !== undefined) entry.validFrom = fields.validFrom;
    if (fields.validTo !== undefined) entry.validTo = fields.validTo == null ? null : fields.validTo;
    if (fields.supersedes !== undefined) entry.supersedes = fields.supersedes || null;
    if (fields.supersededBy !== undefined) entry.supersededBy = fields.supersededBy || null;
    if (fields.consolidatedInto !== undefined) entry.consolidatedInto = fields.consolidatedInto || null;
    if (fields.docId !== undefined) entry.docId = fields.docId == null ? null : String(fields.docId);
    if (fields.title !== undefined) entry.title = fields.title == null ? null : String(fields.title);
    if (fields.chunk !== undefined) entry.chunk = fields.chunk == null ? null : Math.max(0, Math.floor(Number(fields.chunk) || 0));
    if (fields.meta !== undefined) entry.meta = fields.meta && typeof fields.meta === 'object' ? fields.meta : null;
    if (fields.ttlDays !== undefined) entry.ttlDays = fields.ttlDays == null ? null : Number(fields.ttlDays);
    if (fields.importance !== undefined) entry.importance = clampImportance(fields.importance, this.defaultImportance);
    if (fields.seen !== undefined) entry.seen = positiveInteger(fields.seen, entry.seen || 1);
    if (fields.accessCount !== undefined) entry.accessCount = Math.max(0, Math.floor(Number(fields.accessCount) || 0));
    if (fields.lastAccessedAt !== undefined) entry.lastAccessedAt = fields.lastAccessedAt || null;
    if (needsIndex || reindex) this._index(entry);
  }

  _index(entry) {
    this._unindex(entry.id);
    const tokens = this._tokenize(`${entry.text} ${entry.tags.join(' ')} ${entry.title || ''} ${entry.subject || ''} ${entry.predicate || ''} ${entry.value || ''}`);
    const tf = new Map();
    for (const token of tokens) tf.set(token, (tf.get(token) || 0) + 1);
    this._tf.set(entry.id, tf);
    this._len.set(entry.id, tokens.length);
    this._tokens.set(entry.id, tokens);
    this._totalLen += tokens.length;
    for (const token of tf.keys()) this._df.set(token, (this._df.get(token) || 0) + 1);
    const key = this._dedupeKey(entry);
    this._keys.set(key, entry.id);
    this._idKeys.set(entry.id, key);
  }

  _unindex(id) {
    const tf = this._tf.get(id);
    if (!tf) return;
    for (const token of tf.keys()) {
      const count = (this._df.get(token) || 1) - 1;
      if (count <= 0) this._df.delete(token); else this._df.set(token, count);
    }
    this._totalLen -= this._len.get(id) || 0;
    this._tf.delete(id);
    this._len.delete(id);
    this._tokens.delete(id);
    const key = this._idKeys.get(id);
    if (key !== undefined && this._keys.get(key) === id) this._keys.delete(key);
    this._idKeys.delete(id);
  }

  _dedupeKey(entry) {
    return [
      entry.namespace || 'default',
      entry.kind || 'note',
      normalizeKeyText(entry.subject),
      normalizeKeyText(entry.predicate),
      normalizeKeyText(entry.value),
      normalizeKeyText(entry.text),
    ].join('\u0000');
  }

  _kindSet(kinds) {
    if (kinds === undefined || kinds === null) return null;
    const list = Array.isArray(kinds) ? kinds : [kinds];
    return new Set(list.map(String));
  }

  _select({ kinds = RECALL_KINDS, namespace, session, includeHistory = false, docId } = {}) {
    const wanted = this._kindSet(kinds);
    const ns = namespace === undefined ? this.namespace : namespace;
    const sess = session === undefined ? this.session : session;
    const out = [];
    for (const entry of this._docs.values()) {
      if (wanted && !wanted.has(entry.kind)) continue;
      if (ns !== null && entry.namespace !== ns) continue;
      if (sess !== null && entry.session !== sess) continue;
      if (docId !== undefined && docId !== null && entry.docId !== docId) continue;
      if (!includeHistory && !this._active(entry)) continue;
      out.push(entry);
    }
    return out;
  }

  _active(entry) {
    const now = this.now();
    const from = parseTime(entry.validFrom);
    if (from !== null && from > now) return false;
    const until = parseTime(entry.validTo);
    if (until !== null && until <= now) return false;
    return true;
  }

  _versioned(entry) {
    return (entry.kind === 'fact' || entry.kind === 'preference') &&
      entry.subject && entry.predicate && entry.value != null && entry.value !== '';
  }

  _conflicts(candidate) {
    if (!this._versioned(candidate)) return [];
    const value = normalizeKeyText(candidate.value);
    return [...this._docs.values()].filter((entry) =>
      entry.id !== candidate.id &&
      entry.namespace === candidate.namespace &&
      entry.kind === candidate.kind &&
      normalizeKeyText(entry.subject) === normalizeKeyText(candidate.subject) &&
      normalizeKeyText(entry.predicate) === normalizeKeyText(candidate.predicate) &&
      this._active(entry) &&
      normalizeKeyText(entry.value) !== value);
  }

  async _mergeDuplicate(existing, incoming) {
    let vector = null;
    let embedded = !!this._vectors.get(existing.id);
    if (!embedded && this.semantic) {
      const vectors = await this._embedAll([incoming.text]);
      if (vectors && vectors[0]) {
        vector = vectors[0];
        this._append({ t: 'vec', id: existing.id, vec: encodeVector(vector), model: this.model });
        this._setVector(existing.id, vector, this.model);
        embedded = true;
      }
    }
    const merged = this._merge(existing, incoming, vector);
    return { id: existing.id, entry: merged, embedded, merged: true };
  }

  _findExact(candidate) {
    const id = this._keys.get(this._dedupeKey(candidate));
    if (!id) return null;
    const entry = this._docs.get(id);
    return entry && this._active(entry) ? entry : null;
  }

  _findStructured(candidate) {
    if (!this._versioned(candidate)) return null;
    const value = normalizeKeyText(candidate.value);
    for (const entry of this._docs.values()) {
      if (entry.id === candidate.id || entry.namespace !== candidate.namespace || entry.kind !== candidate.kind) continue;
      if (!this._active(entry)) continue;
      if (normalizeKeyText(entry.subject) !== normalizeKeyText(candidate.subject)) continue;
      if (normalizeKeyText(entry.predicate) !== normalizeKeyText(candidate.predicate)) continue;
      if (normalizeKeyText(entry.value) === value) return entry;
    }
    return null;
  }

  _findSemanticDuplicate(candidate, vector) {
    if (!this._versioned(candidate) && candidate.subject && candidate.predicate) return null;
    let best = null;
    let bestScore = -Infinity;
    for (const entry of this._docs.values()) {
      if (entry.id === candidate.id) continue;
      if (entry.namespace !== candidate.namespace || entry.kind !== candidate.kind) continue;
      if (!this._active(entry)) continue;
      if (candidate.subject && candidate.predicate) {
        if (normalizeKeyText(entry.subject) !== normalizeKeyText(candidate.subject)) continue;
        if (normalizeKeyText(entry.predicate) !== normalizeKeyText(candidate.predicate)) continue;
        if (normalizeKeyText(entry.value) !== normalizeKeyText(candidate.value)) continue;
      }
      if (materiallyDifferent(entry.text, candidate.text)) continue;
      const existing = this._vectors.get(entry.id);
      let similarity;
      if (existing && existing.model === this.model && vector && existing.vec.length === vector.length) {
        similarity = dot(normalizeVector(vector), existing.vec);
      } else {
        similarity = jaccard(new Set(this._tokens.get(candidate.id) || []), new Set(this._tokens.get(entry.id) || []));
      }
      if (similarity > bestScore) { bestScore = similarity; best = entry; }
    }
    return best && bestScore >= this.dedupeSimilarity ? best : null;
  }

  _duplicates(a, b, similarity) {
    if (a.id === b.id || a.namespace !== b.namespace || a.kind !== b.kind) return false;
    const structuredSame = a.subject && a.predicate && b.subject && b.predicate;
    if (structuredSame) {
      if (normalizeKeyText(a.subject) !== normalizeKeyText(b.subject)) return false;
      if (normalizeKeyText(a.predicate) !== normalizeKeyText(b.predicate)) return false;
      if (normalizeKeyText(a.value) !== normalizeKeyText(b.value)) return false;
    }
    if (normalizeKeyText(a.text) === normalizeKeyText(b.text)) return true;
    if (!structuredSame && materiallyDifferent(a.text, b.text)) return false;
    return this._similarity(a, b) >= similarity;
  }

  _merge(existing, incoming, vector) {
    const fields = {
      at: incoming.at,
      seen: (existing.seen || 1) + 1,
      importance: clamp01(Math.max(existing.importance, incoming.importance) + 0.03),
      confidence: Math.max(existing.confidence, incoming.confidence),
      tags: [...new Set([...(existing.tags || []), ...(incoming.tags || [])])].slice(0, 8),
      namespace: existing.namespace,
    };
    if (!existing.session && incoming.session) fields.session = incoming.session;
    if (!existing.subject && incoming.subject) fields.subject = incoming.subject;
    if (!existing.predicate && incoming.predicate) fields.predicate = incoming.predicate;
    if (!existing.value && incoming.value) fields.value = incoming.value;
    // Same structured identity with a richer phrasing: keep the more informative text.
    if (incoming.text.length > existing.text.length + 8) fields.text = incoming.text;
    this._updateEntry(existing, fields, false);
    this._append({ t: 'imp', id: existing.id, importance: existing.importance });
    this._append({
      t: 'meta',
      id: existing.id,
      fields: {
        at: existing.at, text: existing.text, seen: existing.seen, confidence: existing.confidence,
        tags: existing.tags, session: existing.session, subject: existing.subject,
        predicate: existing.predicate, value: existing.value,
      },
    });
    if (vector && !this._vectors.has(existing.id)) {
      this._append({ t: 'vec', id: existing.id, vec: encodeVector(vector), model: this.model });
      this._setVector(existing.id, vector, this.model);
    }
    return existing;
  }

  _reinforce(entries) {
    if (!entries.length) return;
    const nowIso = new Date(this.now()).toISOString();
    for (const entry of entries) {
      const live = this._docs.get(entry.id);
      if (!live) continue;
      live.accessCount = (live.accessCount || 0) + 1;
      live.lastAccessedAt = nowIso;
      entry.accessCount = live.accessCount;
      entry.lastAccessedAt = nowIso;
      this._append({ t: 'meta', id: live.id, fields: { accessCount: live.accessCount, lastAccessedAt: nowIso } });
    }
  }

  _salience(entry) {
    if (this.salience) {
      try { return clamp01(this.salience(entry)); } catch { /* fall through */ }
    }
    const usage = Math.min(1, Math.log1p(entry.accessCount || 0) / Math.log1p(20));
    const confidenceAdjustment = 0.05 * ((entry.confidence ?? this.defaultConfidence) - 0.5);
    return clamp01((entry.importance ?? this.defaultImportance) + 0.1 * usage + confidenceAdjustment);
  }

  _decayFor(kind) {
    return this.decayDays[kind] || this.decayDays.default || DEFAULT_DECAY_DAYS.default;
  }

  _tokenize(text) {
    return (String(text).toLowerCase().match(/[a-z0-9$.%]+/g) || [])
      .map((word) => word.replace(/^[.$%]+|[.%]+$/g, ''))
      .filter((word) => word && !STOP.has(word))
      .map((word) => this.alias[word] || stem(word));
  }

  _unembedded() {
    const missing = [];
    for (const entry of this._docs.values()) {
      const vector = this._vectors.get(entry.id);
      if (!vector || vector.model !== this.model) missing.push(entry);
    }
    return missing;
  }

  _candidateList(value) {
    if (Array.isArray(value)) return value;
    if (value && Array.isArray(value.memories)) return value.memories;
    if (value && Array.isArray(value.candidates)) return value.candidates;
    if (value && typeof value === 'object' && typeof value.text === 'string') return [value];
    return [];
  }

  _parseExtraction(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') return this._candidateList(value);
    if (typeof value !== 'string' || !value.trim()) return [];
    let text = value.trim().replace(/^```(?:json)?/i, '').replace(/```$/i, '').trim();
    const start = text.indexOf('[');
    const end = text.lastIndexOf(']');
    if (start >= 0 && end > start) text = text.slice(start, end + 1);
    try {
      return this._candidateList(JSON.parse(text));
    } catch {
      return [];
    }
  }

  _normalizeCandidate(candidate) {
    if (!candidate) return null;
    const raw = typeof candidate === 'string' ? { text: candidate } : candidate;
    const kind = ['fact', 'preference', 'note', 'episode'].includes(raw.kind) ? raw.kind : 'fact';
    const subject = raw.subject == null ? null : String(raw.subject).slice(0, 80);
    const predicate = raw.predicate == null ? null : String(raw.predicate).slice(0, 80);
    const value = raw.value == null ? null : String(raw.value).slice(0, 200);
    let text = String(raw.text == null ? '' : raw.text).replace(/\s+/g, ' ').trim();
    if (!text && subject && predicate && value) text = `${subject} ${predicate} ${value}`;
    if (!text) return null;
    const confidence = clampImportance(raw.confidence, 0.6);
    const importance = clampImportance(raw.importance, Math.min(0.9, 0.5 + confidence * 0.4));
    return {
      kind,
      text: text.slice(0, MAX_TEXT[kind]),
      subject,
      predicate,
      value,
      confidence,
      importance,
    };
  }

  _extractionPrompt(text) {
    return [
      'Extract durable memories from the text below.',
      'Return ONLY a JSON array, no prose, no markdown fences.',
      'Each item: {"kind":"fact|preference|note|episode","text":"self-contained sentence",',
      '"subject":"optional noun","predicate":"optional key","value":"optional value","confidence":0.0}',
      'Prefer durable facts and preferences. Skip transient chatter, questions, and duplicates.',
      'If nothing durable is present, return [].',
      'Text:',
      '"""',
      String(text).slice(0, 8000),
      '"""',
    ].join('\n');
  }

  async _generateText(prompt) {
    try {
      if (this.generate) {
        const result = await this.generate(prompt, { model: this.extractModel });
        return result == null ? null : String(result);
      }
      if (!this.extractModel || !this._fetch || !this.ollamaUrl) return null;
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), this.extractTimeoutMs) : null;
      try {
        const response = await this._fetch(`${this.ollamaUrl}/api/generate`, {
          method: 'POST',
          headers: { 'content-type': 'application/json', ...this.headers },
          body: JSON.stringify({ model: this.extractModel, prompt, stream: false, format: 'json' }),
          signal: controller ? controller.signal : undefined,
          redirect: 'manual',
        });
        if (!response) return null;
        if (isRedirectStatus(response.status)) return null;
        if (!response.ok) return null;
        const data = await response.json();
        return data && typeof data.response === 'string' ? data.response : null;
      } finally {
        if (timer) clearTimeout(timer);
      }
    } catch {
      return null;
    }
  }

  async _embedAll(texts) {
    if (!this.semantic || !Array.isArray(texts) || !texts.length) return null;
    try {
      const output = [];
      for (let i = 0; i < texts.length; i += this.batchSize) {
        const batch = texts.slice(i, i + this.batchSize);
        this._metrics.embedCalls++;
        const vectors = this.embed ? await this.embed(batch) : await this._ollamaEmbed(batch);
        if (!validVectors(vectors, batch.length)) { this._metrics.embedFailures++; return null; }
        output.push(...vectors);
      }
      return output;
    } catch {
      this._metrics.embedFailures++;
      return null;
    }
  }

  async _queryEmbed(text) {
    const key = `${this.model}\u0000${text}`;
    if (this._queryCache.has(key)) {
      const vector = this._queryCache.get(key);
      this._queryCache.delete(key);
      this._queryCache.set(key, vector);
      return vector;
    }
    const vectors = await this._embedAll([text]);
    if (!vectors || !vectors[0]) return null;
    const vector = normalizeVector(vectors[0]);
    if (this.queryCacheSize > 0) {
      this._queryCache.set(key, vector);
      while (this._queryCache.size > this.queryCacheSize) {
        this._queryCache.delete(this._queryCache.keys().next().value);
      }
    }
    return vector;
  }

  async _ollamaEmbed(texts) {
    const batch = await this._requestEmbed('/api/embed', { model: this.model, input: texts });
    if (batch && batch.embeddings) return batch.embeddings;
    if (!batch || !batch.missing) return null;

    const output = [];
    for (const text of texts) {
      const single = await this._requestEmbed('/api/embeddings', { model: this.model, prompt: text });
      if (!single || !single.embeddings || !single.embeddings.length) return null;
      output.push(single.embeddings[0]);
    }
    return output;
  }

  async _requestEmbed(endpoint, payload) {
    if (!this._fetch || !this.ollamaUrl) return null;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), this.timeoutMs) : null;
    try {
      const response = await this._fetch(`${this.ollamaUrl}${endpoint}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', ...this.headers },
        body: JSON.stringify(payload),
        signal: controller ? controller.signal : undefined,
        redirect: 'manual',
      });
      if (!response) return null;
      if (isRedirectStatus(response.status)) return null;
      if (response.status === 404 || response.status === 405) return { missing: true };
      if (!response.ok) return null;
      const data = await response.json();
      if (!data) return null;
      if (endpoint === '/api/embed') {
        return Array.isArray(data.embeddings) ? { embeddings: data.embeddings } : null;
      }
      return Array.isArray(data.embedding) ? { embeddings: [data.embedding] } : null;
    } catch {
      return null;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  saveAnn() {
    if (!this.annPath || !this._ann) return false;
    const fingerprint = crypto.createHash('sha1')
      .update([...this._ann.rows.keys()].sort().join('|') + ':' + this.model)
      .digest('hex');
    const payload = { ...this._ann.serialize(), model: this.model, fingerprint, savedAt: new Date(this.now()).toISOString() };
    const temp = `${this.annPath}.tmp`;
    fs.mkdirSync(path.dirname(this.annPath), { recursive: true });
    fs.writeFileSync(temp, JSON.stringify(payload));
    fs.renameSync(temp, this.annPath);
    return true;
  }

  _ensureAnn() {
    if (this.annThreshold === Infinity || this._vectors.size < this.annThreshold) return null;
    let currentVectors = 0;
    for (const vector of this._vectors.values()) if (vector.model === this.model) currentVectors++;
    if (this._ann && this._ann.size === currentVectors) return this._ann;
    const rows = [];
    for (const [id, vector] of this._vectors) {
      if (vector.model === this.model) rows.push({ id, vec: vector.vec });
    }
    if (rows.length < this.annThreshold) return null;
    if (this.annPath) {
      try {
        const data = JSON.parse(fs.readFileSync(this.annPath, 'utf8'));
        const fingerprint = crypto.createHash('sha1')
          .update(rows.map((row) => row.id).sort().join('|') + ':' + this.model)
          .digest('hex');
        if (data.model === this.model && data.fingerprint === fingerprint && Number(data.size) === rows.length) {
          const restored = IvfIndex.restore(data, rows, { clusters: this.annClusters, rebuildAfter: this.annRebuildAfter });
          if (restored) { this._ann = restored; return this._ann; }
        }
      } catch { /* rebuild below */ }
    }
    this._ann = new IvfIndex(rows, { clusters: this.annClusters, rebuildAfter: this.annRebuildAfter });
    this.saveAnn();
    return this._ann;
  }

  async _rank(items, queryText, queryTokens) {
    const totalDocs = this._docs.size || 1;
    const averageLength = this._totalLen / totalDocs || 1;
    const lexical = [];
    for (const item of items) {
      const tf = this._tf.get(item.id);
      if (!tf) continue;
      const length = this._len.get(item.id) || 1;
      let score = 0;
      for (const token of queryTokens) {
        const count = tf.get(token);
        if (!count) continue;
        const docsWithToken = this._df.get(token) || 0;
        const idf = Math.log(1 + (totalDocs - docsWithToken + 0.5) / (docsWithToken + 0.5));
        score += idf * ((count * 2.4) / (count + 1.4 * (0.25 + 0.75 * (length / averageLength))));
      }
      if (score > 0) lexical.push([item.id, score]);
    }
    lexical.sort((a, b) => b[1] - a[1]);

    let semantic = [];
    let mode = 'lexical';
    const usable = items.filter((item) => {
      const vector = this._vectors.get(item.id);
      return vector && vector.model === this.model;
    });
    if (this.semantic && usable.length) {
      const queryVector = await this._queryEmbed(queryText);
      if (queryVector) {
        let candidates = usable;
        if (usable.length >= this.annThreshold) {
          const ann = this._ensureAnn();
          if (ann) {
            const ids = ann.search(queryVector, { nprobe: this.annNProbe, k: Math.max(RRF_LIMIT * 4, 200) });
            const allowed = new Set(ids);
            const filtered = usable.filter((item) => allowed.has(item.id));
            // Scoped/filtered searches that are too sparse use exact search rather than miss results.
            if (filtered.length >= Math.min(RRF_LIMIT, usable.length)) candidates = filtered;
          }
        }
        semantic = candidates
          .map((item) => {
            const vector = this._vectors.get(item.id);
            if (!vector || vector.vec.length !== queryVector.length) return null;
            return [item.id, dot(queryVector, vector.vec)];
          })
          .filter((pair) => pair && pair[1] > this.minSimilarity)
          .sort((a, b) => b[1] - a[1]);
        mode = usable.length === items.length ? 'hybrid' : 'hybrid-partial';
      }
    }

    const denseRanks = (list) => {
      let rank = -1;
      let previous;
      return list.map(([id, value], index) => {
        if (index === 0 || value !== previous) rank++;
        previous = value;
        return [id, rank];
      });
    };
    const fused = new Map();
    denseRanks(lexical).slice(0, RRF_LIMIT).forEach(([id, rank]) => {
      fused.set(id, (fused.get(id) || 0) + 1 / (RRF_K + rank));
    });
    denseRanks(semantic).slice(0, RRF_LIMIT).forEach(([id, rank]) => {
      fused.set(id, (fused.get(id) || 0) + 1 / (RRF_K + rank));
    });
    const ranked = [...fused.entries()].sort((a, b) => b[1] - a[1]);
    return { ranked, mode };
  }

  async _applyRerank(query, ranked) {
    const candidates = ranked.slice(0, this.rerankCandidates)
      .map(([id, score]) => ({ id, score, ...this._docs.get(id) }))
      .filter((candidate) => candidate.text);
    if (!candidates.length) return ranked;
    try {
      const result = await this.rerank({ query, candidates });
      if (!Array.isArray(result) || !result.length) return ranked;
      const byId = new Map();
      result.forEach((item, index) => {
        if (typeof item === 'string') byId.set(item, 1 / (index + 1));
        else if (item && typeof item.id === 'string') byId.set(item.id, Number.isFinite(Number(item.score)) ? Number(item.score) : 1 / (index + 1));
      });
      const reranked = [...byId.entries()].filter(([id]) => this._docs.has(id)).sort((a, b) => b[1] - a[1]);
      if (!reranked.length) return ranked;
      const seen = new Set(reranked.map(([id]) => id));
      return reranked.concat(ranked.filter(([id]) => !seen.has(id) && !byId.has(id)));
    } catch {
      return ranked;
    }
  }

  _similarity(a, b) {
    const va = this._vectors.get(a.id);
    const vb = this._vectors.get(b.id);
    if (va && vb && va.model === vb.model && va.vec.length === vb.vec.length) {
      return dot(va.vec, vb.vec);
    }
    return jaccard(new Set(this._tokens.get(a.id) || []), new Set(this._tokens.get(b.id) || []));
  }

  _compactIfNeeded(force = false) {
    let size = 0;
    try { size = this.storage.size(); } catch { size = 0; }
    if (!force && size < this.compactBytes && this._dead < Math.max(200, this._docs.size)) return false;
    const run = () => this._rewrite();
    if (!this._fileStorage || !this.file) return run();
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    return this._withFileLock(`${this.file}.lock`, run);
  }

  _withFileLock(lockFile, fn) {
    const started = Date.now();
    let acquired = false;
    while (!acquired) {
      try {
        const fd = fs.openSync(lockFile, 'wx');
        fs.writeFileSync(fd, `${process.pid} ${Date.now()}`);
        fs.closeSync(fd);
        acquired = true;
      } catch (error) {
        if (error.code !== 'EEXIST') return false;
        try {
          const stat = fs.statSync(lockFile);
          if (Date.now() - stat.mtimeMs > this.lockStaleMs) {
            fs.unlinkSync(lockFile);
            continue;
          }
        } catch { /* lock disappeared */ }
        if (Date.now() - started > this.lockWaitMs) return false;
        sleepMs(this.lockRetryMs);
      }
    }
    try {
      return fn();
    } finally {
      try { fs.unlinkSync(lockFile); } catch { /* already gone */ }
    }
  }

  _rewrite() {
    const all = [...this._docs.values()];
    const turns = all.filter((entry) => entry.kind === 'turn')
      .sort((a, b) => (a.at < b.at ? -1 : 1));
    const drop = new Set(turns.slice(0, Math.max(0, turns.length - this.maxTurns)).map((entry) => entry.id));
    const keep = all.filter((entry) => !drop.has(entry.id))
      .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    const records = [{ t: 'schema', v: this.schemaVersion }];
    for (const entry of keep) {
      records.push(this._entryRecord(entry));
      if (entry.importance !== this.defaultImportance) {
        records.push({ t: 'imp', id: entry.id, importance: entry.importance });
      }
      const vector = this._vectors.get(entry.id);
      if (vector) records.push({ t: 'vec', id: entry.id, vec: encodeVector(vector.vec), model: vector.model });
    }
    this.storage.rewrite(records);
    this._schemaWritten = true;
    for (const id of drop) {
      this._docs.delete(id);
      this._vectors.delete(id);
      if (this._ann) this._ann.remove(id);
      this._unindex(id);
      this._seq.delete(id);
    }
    this._dead = 0;
    this._lines = records.length;
    return true;
  }
}

module.exports = UniversalMemory;
module.exports.UniversalMemory = UniversalMemory;
module.exports.JsonlStorage = JsonlStorage;
module.exports.SqliteStorage = SqliteStorage;
module.exports.EncryptedJsonlStorage = EncryptedJsonlStorage;
module.exports.IvfIndex = IvfIndex;
module.exports.DEFAULT_ALIAS = DEFAULT_ALIAS;
module.exports.DEFAULT_DECAY_DAYS = DEFAULT_DECAY_DAYS;
module.exports.chunkText = chunkText;
module.exports.htmlToText = htmlToText;
module.exports.redactPII = redactPII;
module.exports.extractPdfText = extractPdfText;
module.exports.tesseractOcr = tesseractOcr;
module.exports.popplerRenderer = popplerRenderer;
module.exports.popplerTextExtractor = popplerTextExtractor;
module.exports.imageMime = imageMime;
module.exports.extractOfficeText = extractOfficeText;
module.exports.default = UniversalMemory;
