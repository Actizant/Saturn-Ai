// Drop-in HTTP memory server for non-Node clients or a standalone memory sidecar.
// No framework, no dependencies.
'use strict';

const http = require('http');
const https = require('https');
const crypto = require('crypto');
const { URL } = require('url');

const MAX_BODY = 10 * 1024 * 1024;

function readJson(request, limit = MAX_BODY) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        const error = new Error('BODY_TOO_LARGE');
        error.code = 'BODY_TOO_LARGE';
        request.destroy();
        reject(error);
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (!chunks.length) return resolve({});
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch (error) {
        error.code = 'BAD_JSON';
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

function send(response, status, body) {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
    'cache-control': 'no-store',
  });
  response.end(payload);
}

function authorized(request, apiKey) {
  if (!apiKey) return true;
  const header = String(request.headers.authorization || '');
  const bearer = header.startsWith('Bearer ') ? header.slice(7) : '';
  return bearer === apiKey || request.headers['x-api-key'] === apiKey;
}

/**
 * Create an HTTP server around a UniversalMemory instance.
 * Routes:
 *   GET    /health
 *   POST   /remember
 *   POST   /ingest
 *   POST   /recall
 *   POST   /context
 *   POST   /feedback
 *   GET    /documents
 *   POST   /documents
 *   DELETE /documents/:docId
 *   DELETE /memory/:id
 */
function createMemoryServer({ memory, apiKey = null, maxBodyBytes = MAX_BODY, rateLimit = null, cors = null, tls = null } = {}) {
  if (!memory || typeof memory.remember !== 'function') throw new TypeError('createMemoryServer requires a UniversalMemory instance');
  const windows = new Map();
  const corsOrigin = cors === true ? '*' : (typeof cors === 'string' ? cors : (cors && cors.origin) || null);
  const corsHeaders = 'authorization, content-type, x-api-key';

  const limited = (request, response) => {
    if (!rateLimit) return false;
    const windowMs = Math.max(1000, Number(rateLimit.windowMs) || 60000);
    const max = Math.max(1, Number(rateLimit.max) || 120);
    const key = String(request.socket.remoteAddress || 'unknown');
    const now = Date.now();
    let bucket = windows.get(key);
    if (!bucket || now - bucket.start >= windowMs) {
      bucket = { start: now, count: 0 };
      windows.set(key, bucket);
    }
    bucket.count++;
    if (bucket.count <= max) return false;
    response.setHeader('retry-after', String(Math.ceil((bucket.start + windowMs - now) / 1000)));
    send(response, 429, { error: 'RATE_LIMITED' });
    return true;
  };

  const handle = async (request, response) => {
    response.setHeader('x-request-id', crypto.randomUUID());
    if (corsOrigin) {
      response.setHeader('access-control-allow-origin', corsOrigin);
      response.setHeader('access-control-allow-headers', corsHeaders);
      response.setHeader('access-control-allow-methods', 'GET, POST, DELETE, OPTIONS');
      response.setHeader('vary', 'origin');
    }
    if (request.method === 'OPTIONS') { response.writeHead(204); response.end(); return; }
    try {
      const url = new URL(request.url, 'http://localhost');
      const pathname = url.pathname.replace(/\/+$/, '') || '/';
      if (pathname === '/health' && request.method === 'GET') {
        return send(response, 200, { ok: true, ...memory.stats(), metrics: memory.metrics() });
      }
      if (limited(request, response)) return;
      if (!authorized(request, apiKey)) return send(response, 401, { error: 'UNAUTHORIZED' });
      if (pathname === '/metrics' && request.method === 'GET') {
        return send(response, 200, memory.metrics());
      }
      if (pathname === '/export' && request.method === 'GET') {
        return send(response, 200, memory.export({ includeVectors: false, includeHistory: false }));
      }
      if (pathname === '/import' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!Array.isArray(body.entries)) return send(response, 400, { error: 'ENTRIES_REQUIRED' });
        const result = await memory.import(body.entries, { preserveIds: true });
        return send(response, 200, result);
      }

      if (pathname === '/remember' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.text) return send(response, 400, { error: 'TEXT_REQUIRED' });
        const result = await memory.remember(body.text, body);
        return send(response, result.error ? 400 : 200, result);
      }
      if (pathname === '/ingest' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.text) return send(response, 400, { error: 'TEXT_REQUIRED' });
        const result = await memory.ingest(body.text, body);
        return send(response, result.error ? 400 : 200, result);
      }
      if (pathname === '/recall' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.query) return send(response, 400, { error: 'QUERY_REQUIRED' });
        const hits = await memory.recall(body.query, body);
        return send(response, 200, { hits, mode: memory.lastMode });
      }
      if (pathname === '/context' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.query) return send(response, 400, { error: 'QUERY_REQUIRED' });
        const context = await memory.context(body.query, body);
        return send(response, 200, context);
      }
      if (pathname === '/feedback' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.id) return send(response, 400, { error: 'ID_REQUIRED' });
        const result = memory.feedback(body.id, body);
        return send(response, result.error ? 404 : 200, result);
      }
      if (pathname === '/documents' && request.method === 'GET') {
        const all = memory.documents(url.searchParams.get('namespace') || undefined);
        const limit = Math.max(0, Number(url.searchParams.get('limit')) || 0);
        const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
        const documents = limit ? all.slice(offset, offset + limit) : all.slice(offset);
        return send(response, 200, { documents, total: all.length, limit: limit || null, offset });
      }
      if (pathname === '/documents' && request.method === 'POST') {
        const body = await readJson(request, maxBodyBytes);
        if (!body.text) return send(response, 400, { error: 'TEXT_REQUIRED' });
        const result = await memory.ingestDocument(body.text, body);
        return send(response, result.error ? 400 : 200, result);
      }
      if (pathname.startsWith('/documents/') && request.method === 'DELETE') {
        const docId = decodeURIComponent(pathname.slice('/documents/'.length));
        const result = memory.removeDocument(docId);
        return send(response, result.deleted ? 200 : 404, result);
      }
      if (pathname.startsWith('/memory/') && request.method === 'DELETE') {
        const id = decodeURIComponent(pathname.slice('/memory/'.length));
        const result = memory.forget(id);
        return send(response, result.error ? 404 : 200, result);
      }
      return send(response, 404, { error: 'NOT_FOUND' });
    } catch (error) {
      if (error && error.code === 'BODY_TOO_LARGE') return send(response, 413, { error: 'BODY_TOO_LARGE' });
      if (error && error.code === 'BAD_JSON') return send(response, 400, { error: 'BAD_JSON' });
      if (error && error.code === 'FORBIDDEN') return send(response, 403, { error: 'FORBIDDEN' });
      return send(response, 500, { error: 'INTERNAL', message: String(error.message || error) });
    }
  };
  const server = tls ? https.createServer(tls, handle) : http.createServer(handle);
  server.memory = memory;
  return server;
}

module.exports = { createMemoryServer };
