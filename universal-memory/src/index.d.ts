export type MemoryKind = 'note' | 'turn' | 'episode' | 'fact' | 'preference' | 'summary';

export interface MemoryWeights {
  relevance: number;
  recency: number;
  importance: number;
}

export interface DecayDays {
  turn?: number;
  episode?: number;
  note?: number;
  fact?: number;
  preference?: number;
  summary?: number;
  default?: number;
  [kind: string]: number | undefined;
}

export interface MemoryEntry {
  id: string;
  at: string;
  kind: MemoryKind | string;
  text: string;
  tags: string[];
  src: string;
  namespace: string;
  session: string | null;
  subject: string | null;
  predicate: string | null;
  value: string | null;
  confidence: number;
  validFrom: string;
  validTo: string | null;
  supersedes: string | null;
  supersededBy: string | null;
  consolidatedInto: string | null;
  ttlDays: number | null;
  docId: string | null;
  title: string | null;
  chunk: number | null;
  meta: Record<string, unknown> | null;
  importance: number;
  seen: number;
  accessCount: number;
  lastAccessedAt: string | null;
}

export interface RecallHit extends MemoryEntry {
  score: number;
  relevance: number;
  recency: number;
  importanceScore: number;
  cite?: string;
}

export interface RememberOptions {
  kind?: MemoryKind | string;
  tags?: string[];
  src?: string;
  at?: string;
  importance?: number;
  namespace?: string;
  session?: string | null;
  subject?: string;
  predicate?: string;
  value?: string | number | boolean;
  confidence?: number;
  validFrom?: string;
  validTo?: string;
  ttlDays?: number;
  dedupe?: boolean;
  docId?: string | null;
  title?: string | null;
  chunk?: number | null;
  meta?: Record<string, unknown> | null;
}

export interface RememberResult {
  id?: string;
  entry?: MemoryEntry;
  embedded?: boolean;
  merged?: boolean;
  pending?: boolean;
  superseded?: string[];
  error?: 'EMPTY' | 'CONTRADICTION' | string;
  conflicts?: string[];
}

export interface RecallOptions {
  k?: number;
  kinds?: string[] | string | null;
  namespace?: string | null;
  session?: string | null;
  includeHistory?: boolean;
  reinforce?: boolean;
  minScore?: number | null;
  rerank?: boolean;
  route?: boolean;
  docId?: string | null;
}

export interface ContextOptions extends RecallOptions {
  tokenBudget?: number;
  mmrLambda?: number;
  perSubject?: number;
  format?: 'text' | 'xml';
  redactPII?: boolean;
  mmr?: boolean;
  modelContextWindow?: number;
  contextWindow?: number;
}

export interface ContextResult {
  text: string;
  entries: RecallHit[];
  tokens: number;
  truncated: boolean;
  mode: string;
  citations: string[];
}

export interface MemoryCandidate {
  kind: 'fact' | 'preference' | 'note' | 'episode' | string;
  text: string;
  subject?: string | null;
  predicate?: string | null;
  value?: string | null;
  confidence?: number;
  importance?: number;
}

export interface ExtractOptions {
  namespace?: string;
  session?: string | null;
  extractor?: ((text: string, context: object) => Promise<unknown> | unknown) | null;
}

export interface IngestOptions extends RememberOptions {
  extract?: boolean;
  extractor?: ((text: string, context: object) => Promise<unknown> | unknown) | null;
}

export interface IngestResult {
  turn?: RememberResult;
  extracted: string[];
  candidates: MemoryCandidate[];
  errors: Array<RememberResult>;
  error?: string;
}

export interface StorageAdapter {
  file?: string | null;
  load(): object[];
  append(record: object): void;
  rewrite(records: object[]): void;
  size(): number;
}

export interface DocumentOptions {
  title?: string;
  source?: string;
  namespace?: string;
  session?: string | null;
  chunkSize?: number;
  chunkOverlap?: number;
  importance?: number;
  confidence?: number;
  tags?: string[];
  metadata?: Record<string, unknown> | null;
  docId?: string | null;
  replace?: boolean;
  at?: string;
  contentType?: string;
  parser?: Function;
  documentParser?: Function;
  ocr?: ((buffer: Uint8Array, meta: { contentType?: string; file?: string; url?: string }) => Promise<string> | string) | null;
  pdfRenderer?: ((buffer: Uint8Array, meta: { file?: string; url?: string; password?: string }) => Promise<Uint8Array[]> | Uint8Array[]) | null;
  pdfTextExtractor?: ((buffer: Uint8Array, meta: { file?: string; url?: string; password?: string }) => Promise<string> | string) | null;
  pdfPassword?: string;
  pdfLayout?: boolean;
  pdftotextCommand?: string;
  headers?: Record<string, string>;
}

export interface DocumentResult {
  docId: string | null;
  title: string;
  chunks: number;
  ids: string[];
  errors: Array<RememberResult>;
  error?: string;
  message?: string;
  file?: string;
  url?: string;
  status?: number | null;
  contentType?: string;
}

export interface DocumentInfo {
  docId: string;
  title: string;
  source: string;
  namespace: string;
  chunks: number;
  at: string;
  meta: Record<string, unknown> | null;
}

export interface MemoryMetrics {
  remembers: number;
  recalls: number;
  contexts: number;
  documents: number;
  embedCalls: number;
  embedFailures: number;
  embedRetries: number;
  recalledHits: number;
  recalledMisses: number;
  pendingEmbeddings: number;
  memoryCount: number;
  uptimeMs: number;
  mode: string;
}

export interface PdfPage {
  index: number;
  text: string;
}

export interface PdfResult {
  text: string;
  pages: PdfPage[];
  pageCount: number;
  title: string;
  encrypted: boolean;
  needsOcr: boolean;
  error?: string;
  message?: string;
}

export interface OfficeResult {
  text: string;
  title: string;
  kind: string;
  error?: string;
  message?: string;
}

export interface RerankCandidate {
  id: string;
  score: number;
  text: string;
  [key: string]: unknown;
}

export interface UniversalMemoryOptions {
  file?: string;
  storagePath?: string;
  path?: string;
  storage?: StorageAdapter;
  namespace?: string;
  session?: string | null;
  ollamaUrl?: string | null;
  ollamaHost?: string;
  baseUrl?: string;
  apiKey?: string;
  headers?: Record<string, string>;
  model?: string;
  embedModel?: string;
  embeddingModel?: string;
  embed?: ((texts: string[]) => Promise<Array<ArrayLike<number>>>) | null | false;
  documentHeaders?: Record<string, string>;
  documentParser?: ((buffer: Uint8Array, meta: { file?: string; url?: string; ext?: string; contentType?: string }) => Promise<string | { text: string; title?: string }> | string | { text: string; title?: string }) | null;
  ocr?: ((buffer: Uint8Array, meta: { contentType?: string; file?: string; url?: string }) => Promise<string> | string) | null;
  pdfRenderer?: ((buffer: Uint8Array, meta: { file?: string; url?: string; password?: string }) => Promise<Uint8Array[]> | Uint8Array[]) | null;
  pdfTextExtractor?: ((buffer: Uint8Array, meta: { file?: string; url?: string; password?: string }) => Promise<string> | string) | null;
  pdfPassword?: string;
  pdfLayout?: boolean;
  pdftotextCommand?: string;
  documentTimeoutMs?: number;
  onEvent?: ((event: { type: string; at: string; [key: string]: unknown }) => void) | null;
  authorize?: ((action: string, context: { namespace?: string | null; kind?: string; id?: string; query?: string }) => boolean) | null;
  fetch?: ((input: string, init?: unknown) => Promise<any>) | null;
  timeoutMs?: number;
  batchSize?: number;
  deferEmbedding?: boolean;
  embedRetries?: number;
  autoResumeEmbeddings?: boolean;
  contextTokenBudget?: number;
  contextK?: number;
  contextMmr?: boolean;
  modelContextWindow?: number;
  contextWindow?: number;
  contextBudgetRatio?: number;
  contextReserveTokens?: number;
  contextMaxTokens?: number;
  minSimilarity?: number;
  weights?: Partial<MemoryWeights>;
  relevance?: number;
  recency?: number;
  importance?: number;
  decayDays?: number | DecayDays;
  recencyDecayDays?: number | DecayDays;
  alias?: Record<string, string>;
  aliases?: Record<string, string>;
  defaultImportance?: number;
  defaultConfidence?: number;
  dedupe?: boolean;
  dedupeSimilarity?: number;
  dedupeKinds?: string[];
  contradictionPolicy?: 'version' | 'reject' | 'keep';
  reinforceOnRecall?: boolean;
  feedbackStep?: number;
  rerank?: ((input: { query: string; candidates: RerankCandidate[] }) => Promise<Array<string | { id: string; score?: number }>> | Array<string | { id: string; score?: number }>) | null;
  rerankCandidates?: number;
  summarizer?: ((texts: string[], entries: MemoryEntry[]) => Promise<string> | string) | null;
  extractor?: ((text: string, context: object) => Promise<unknown> | unknown) | null;
  extractModel?: string | null;
  generate?: ((prompt: string, options?: object) => Promise<string> | string) | null;
  extractTimeoutMs?: number;
  heuristicExtractor?: boolean;
  maxExtracted?: number;
  countTokens?: (text: string) => number;
  salience?: (entry: MemoryEntry) => number;
  queryCacheSize?: number;
  annThreshold?: number;
  annNProbe?: number;
  annClusters?: number;
  annRebuildAfter?: number;
  annPersist?: boolean | string;
  ann?: boolean;
  maxTurns?: number;
  compactBytes?: number;
  lockStaleMs?: number;
  lockWaitMs?: number;
  lockRetryMs?: number;
  fsync?: boolean;
  writeLock?: boolean;
  checksums?: boolean;
  encryptionKey?: string | Uint8Array;
  now?: () => number;
}

export interface MemoryStats {
  count: number;
  byKind: Record<string, number>;
  bytes: number;
  embedded: number;
  pending: number;
  model: string | null;
  active: number;
  archived: number;
  accesses: number;
  namespaces: number;
  schemaVersion: number;
  ann: { size: number; clusters: number; nprobe: number } | null;
  annThreshold: number | null;
}

export interface ExportData {
  schemaVersion: number;
  exportedAt: string;
  entries: Array<MemoryEntry & { vec?: string; vecModel?: string }>;
}

export interface ConsolidateOptions {
  olderThanDays?: number;
  kinds?: string[];
  minGroupSize?: number;
  similarity?: number;
  summarizer?: ((texts: string[], entries: MemoryEntry[]) => Promise<string> | string) | null;
  extract?: boolean;
  extractor?: ((text: string, context: object) => Promise<unknown> | unknown) | null;
  namespace?: string | null;
  dryRun?: boolean;
  maxScan?: number;
}

export interface ConsolidateResult {
  groups: number;
  summaries: string[];
  archived: number;
  extracted: string[];
  dryRun: boolean;
}

export default class UniversalMemory {
  constructor(options?: UniversalMemoryOptions | string);

  readonly file: string | null;
  readonly storage: StorageAdapter;
  namespace: string;
  session: string | null;
  ollamaUrl: string;
  model: string;
  weights: MemoryWeights;
  decayDays: DecayDays;
  defaultImportance: number;
  defaultConfidence: number;
  semantic: boolean;
  lastMode: 'hybrid' | 'hybrid-partial' | 'lexical' | string;
  schemaVersion: number;

  remember(text: string, options?: RememberOptions): Promise<RememberResult>;
  ingest(text: string, options?: IngestOptions): Promise<IngestResult>;
  ingestDocument(content: string, options?: DocumentOptions): Promise<DocumentResult>;
  ingestFile(file: string, options?: DocumentOptions & { contentType?: string; parser?: Function }): Promise<DocumentResult>;
  ingestUrl(url: string, options?: DocumentOptions & { headers?: Record<string, string>; parser?: Function }): Promise<DocumentResult>;
  documents(namespace?: string | null): DocumentInfo[];
  removeDocument(docId: string, options?: { namespace?: string | null }): { deleted: number };
  extract(text: string, options?: ExtractOptions): Promise<MemoryCandidate[]>;
  recall(query: string, options?: RecallOptions): Promise<RecallHit[]>;
  context(query: string, options?: ContextOptions): Promise<ContextResult>;
  contextBudgetFor(modelContextWindow?: number): number;
  inferKinds(query: string): string[];

  forget(id: string): { ok?: boolean; error?: string };
  feedback(id: string, options?: { useful?: boolean; delta?: number }): { ok?: boolean; entry?: MemoryEntry; error?: string };
  history(subject: string, predicate: string, options?: { namespace?: string | null; kind?: string | null }): MemoryEntry[];
  get(id: string): MemoryEntry | null;

  recent(n?: number, options?: { kinds?: string[] | null; namespace?: string | null; session?: string | null; includeHistory?: boolean } | string[]): MemoryEntry[];
  turns(n?: number, options?: { namespace?: string | null; session?: string | null; includeHistory?: boolean }): MemoryEntry[];
  count(kinds?: string[] | string | null, options?: { namespace?: string | null; session?: string | null; includeHistory?: boolean }): number;
  namespaces(): string[];
  stats(namespace?: string | null): MemoryStats;

  compact(): boolean;
  reindex(): Promise<{ embedded: number; pending: number; error?: string }>;
  pendingEmbeddings(): number;
  resumeEmbeddings(): Promise<{ queued: number }>;
  flushEmbeddings(): Promise<{ pending: number }>;
  metrics(): MemoryMetrics;
  saveAnn(): boolean;
  rotateEncryptionKey(newKey: string | Uint8Array): { ok?: boolean; error?: string; file?: string };
  dedupe(options?: { kinds?: string[]; namespace?: string | null; similarity?: number; dryRun?: boolean; maxScan?: number }): Promise<{ merged: number; dryRun: boolean }>;
  consolidate(options?: ConsolidateOptions): Promise<ConsolidateResult>;

  purge(filter?: {
    namespace?: string | null;
    kind?: string;
    kinds?: string[] | string;
    ids?: string | string[];
    subject?: string;
    predicate?: string;
    olderThanDays?: number;
    expiredOnly?: boolean;
  }): { deleted: number };

  export(options?: { namespace?: string | null; includeVectors?: boolean; includeHistory?: boolean }): ExportData;
  import(data: ExportData | MemoryEntry[] | { entries: MemoryEntry[] }, options?: { namespace?: string | null; preserveIds?: boolean }): Promise<{ imported: number; skipped: number }>;

  clearQueryCache(): void;
  close(): void;
}

export { UniversalMemory };
export class JsonlStorage implements StorageAdapter {
  constructor(file: string, options?: { fsync?: boolean; checksums?: boolean });
  file: string;
  load(): object[];
  append(record: object): void;
  rewrite(records: object[]): void;
  size(): number;
  close(): void;
}

export class SqliteStorage implements StorageAdapter {
  constructor(file: string, options?: { table?: string });
  file: string;
  load(): object[];
  append(record: object): void;
  rewrite(records: object[]): void;
  size(): number;
  close(): void;
}

export class EncryptedJsonlStorage implements StorageAdapter {
  constructor(file: string, options: { key: string | Uint8Array; fsync?: boolean });
  file: string;
  load(): object[];
  append(record: object): void;
  rewrite(records: object[]): void;
  size(): number;
  rekey(newKey: string | Uint8Array): string;
  close(): void;
}

export class IvfIndex {  constructor(rows: Array<{ id: string; vec: ArrayLike<number> }>, options?: {
    clusters?: number;
    iterations?: number;
    seed?: number;
    rebuildAfter?: number;
  });
  readonly size: number;
  centroids: ArrayLike<number>[];
  add(id: string, vector: ArrayLike<number>): void;
  remove(id: string): void;
  search(query: ArrayLike<number>, options?: { nprobe?: number; k?: number }): Array<[string, number]>;
}

export const DEFAULT_ALIAS: Readonly<Record<string, string>>;
export const DEFAULT_DECAY_DAYS: Readonly<DecayDays>;
export function chunkText(text: string, options?: { size?: number; overlap?: number }): string[];
export function htmlToText(html: string): string;
export function redactPII(text: string): string;
export function extractPdfText(buffer: Uint8Array): PdfResult;
export function tesseractOcr(options?: {
  command?: string;
  language?: string;
  psm?: number;
  timeoutMs?: number;
  dpi?: number;
  config?: string[];
  preprocess?: (buffer: Uint8Array, meta: { contentType?: string }) => Uint8Array | Promise<Uint8Array>;
}): (buffer: Uint8Array, meta?: { contentType?: string }) => Promise<string>;
export function popplerRenderer(options?: {
  command?: string;
  dpi?: number;
  timeoutMs?: number;
  grayscale?: boolean;
  format?: 'png' | 'jpeg' | 'tiff';
}): (buffer: Uint8Array, meta?: { file?: string; url?: string; password?: string }) => Promise<Uint8Array[]>;
export function popplerTextExtractor(options?: {
  command?: string;
  layout?: boolean;
  timeoutMs?: number;
}): (buffer: Uint8Array, meta?: { file?: string; url?: string; password?: string }) => Promise<string>;
export function imageMime(file: string): string;
export function extractOfficeText(buffer: Uint8Array, options?: { ext?: string }): OfficeResult;
