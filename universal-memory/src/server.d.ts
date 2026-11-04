export interface MemoryServerOptions {
  memory: import('./index.js').default;
  apiKey?: string | null;
  maxBodyBytes?: number;
  cors?: boolean | string | { origin?: string } | null;
  rateLimit?: { windowMs?: number; max?: number } | null;
  tls?: { key: string | Uint8Array; cert: string | Uint8Array } | null;
}

export interface MemoryServer {
  memory: import('./index.js').default;
  listen(port: number, callback?: () => void): unknown;
  close(callback?: (error?: Error) => void): unknown;
}

export function createMemoryServer(options: MemoryServerOptions): MemoryServer;
