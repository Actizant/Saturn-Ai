// Native PDF text extraction. No PDF library, no framework.
//
// Supports the common text-PDF path: top-level objects, object streams, page trees,
// Flate/ASCII85/ASCIIHex/RunLength filters, and text operators (Tj, TJ, ', ").
// It intentionally does not render pages; scanned PDFs return needsOcr=true so the
// caller can supply an OCR engine/renderer.
'use strict';

const zlib = require('zlib');

function ascii85(buffer) {
  const input = buffer.toString('latin1').replace(/\s+/g, '').replace(/^<~/, '').replace(/~>$/, '');
  const output = [];
  let group = [];
  for (const char of input) {
    if (char === 'z' && group.length === 0) { output.push(0, 0, 0, 0); continue; }
    const code = char.charCodeAt(0);
    if (code < 33 || code > 117) continue;
    group.push(code - 33);
    if (group.length === 5) {
      let value = 0;
      for (const digit of group) value = value * 85 + digit;
      output.push((value >>> 24) & 255, (value >>> 16) & 255, (value >>> 8) & 255, value & 255);
      group = [];
    }
  }
  if (group.length) {
    const missing = 5 - group.length;
    while (group.length < 5) group.push(84);
    let value = 0;
    for (const digit of group) value = value * 85 + digit;
    const bytes = [(value >>> 24) & 255, (value >>> 16) & 255, (value >>> 8) & 255, value & 255];
    output.push(...bytes.slice(0, 4 - missing));
  }
  return Buffer.from(output);
}

function asciiHex(buffer) {
  const input = buffer.toString('latin1').replace(/\s+/g, '').replace(/>$/, '');
  const output = [];
  for (let i = 0; i < input.length; i += 2) {
    const pair = input.slice(i, i + 2);
    output.push(parseInt(pair.length === 1 ? `${pair}0` : pair, 16) || 0);
  }
  return Buffer.from(output);
}

function runLength(buffer) {
  const output = [];
  let i = 0;
  while (i < buffer.length) {
    const length = buffer[i++];
    if (length === 128) break;
    if (length < 128) {
      for (let j = 0; j <= length && i < buffer.length; j++) output.push(buffer[i++]);
    } else {
      const value = buffer[i++];
      for (let j = 0; j < 257 - length; j++) output.push(value);
    }
  }
  return Buffer.from(output);
}

function decodeStream(stream, dict) {
  let value = stream;
  const filterMatch = dict.match(/\/Filter\s*(\[[^\]]*\]|\/[A-Za-z0-9]+)/);
  if (!filterMatch) return value;
  const filters = [...filterMatch[1].matchAll(/\/([A-Za-z0-9]+)/g)].map((match) => match[1]);
  for (const filter of filters) {
    try {
      if (filter === 'FlateDecode' || filter === 'Fl') value = zlib.inflateSync(value);
      else if (filter === 'ASCII85Decode' || filter === 'A85') value = ascii85(value);
      else if (filter === 'ASCIIHexDecode' || filter === 'AHx') value = asciiHex(value);
      else if (filter === 'RunLengthDecode' || filter === 'RL') value = runLength(value);
      else return null;
    } catch {
      return null;
    }
  }
  return value;
}

function pdfLiteralString(input, start) {
  let depth = 1;
  let index = start + 1;
  const bytes = [];
  while (index < input.length && depth > 0) {
    const char = input[index++];
    if (char === '\\') {
      const next = input[index++];
      if (next === 'n') bytes.push(10);
      else if (next === 'r') bytes.push(13);
      else if (next === 't') bytes.push(9);
      else if (next === 'b') bytes.push(8);
      else if (next === 'f') bytes.push(12);
      else if (next === '(' || next === ')' || next === '\\') bytes.push(next.charCodeAt(0));
      else if (next === '\n') { /* line continuation */ }
      else if (next === '\r') { if (input[index] === '\n') index++; }
      else if (next >= '0' && next <= '7') {
        let octal = next;
        for (let k = 0; k < 2 && input[index] >= '0' && input[index] <= '7'; k++) octal += input[index++];
        bytes.push(parseInt(octal, 8) & 255);
      } else bytes.push(next.charCodeAt(0));
    } else if (char === '(') { depth++; bytes.push(40); }
    else if (char === ')') { depth--; if (depth > 0) bytes.push(41); }
    else bytes.push(char.charCodeAt(0));
  }
  return { bytes, end: index };
}

function decodePdfBytes(bytes) {
  if (!bytes.length) return '';
  if (bytes.length >= 2 && bytes[0] === 0xFE && bytes[1] === 0xFF) {
    let out = '';
    for (let i = 2; i + 1 < bytes.length; i += 2) out += String.fromCharCode((bytes[i] << 8) | bytes[i + 1]);
    return out;
  }
  if (bytes.length >= 2 && bytes[0] === 0xFF && bytes[1] === 0xFE) {
    let out = '';
    for (let i = 2; i + 1 < bytes.length; i += 2) out += String.fromCharCode(bytes[i] | (bytes[i + 1] << 8));
    return out;
  }
  return Buffer.from(bytes).toString('latin1')
    .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '')
    .replace(/[\x80-\x9F]/g, '');
}

function hexBytes(value) {
  const clean = String(value || '').replace(/\s+/g, '');
  const bytes = [];
  for (let i = 0; i < clean.length; i += 2) bytes.push(parseInt(clean.slice(i, i + 2).padEnd(2, '0'), 16) || 0);
  return bytes;
}

function decodeUtf16(bytes) {
  let out = '';
  for (let i = 0; i + 1 < bytes.length; i += 2) out += String.fromCharCode((bytes[i] << 8) | bytes[i + 1]);
  return out || String.fromCharCode(...bytes);
}

function parseCMap(buffer) {
  const map = new Map();
  const text = buffer.toString('latin1');
  for (const block of text.matchAll(/beginbfchar([\s\S]*?)endbfchar/g)) {
    for (const pair of block[1].matchAll(/<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>/g)) {
      map.set(pair[1].toUpperCase(), decodeUtf16(hexBytes(pair[2])));
    }
  }
  for (const block of text.matchAll(/beginbfrange([\s\S]*?)endbfrange/g)) {
    for (const range of block[1].matchAll(/<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>/g)) {
      const start = parseInt(range[1], 16);
      const end = parseInt(range[2], 16);
      const target = hexBytes(range[3]);
      const width = range[1].length;
      const base = target.length >= 2 ? (target[target.length - 2] << 8) | target[target.length - 1] : target[0];
      for (let code = start; code <= end && code - start < 65536; code++) {
        map.set(code.toString(16).padStart(width, '0').toUpperCase(), String.fromCharCode(base + (code - start)));
      }
    }
  }
  return map;
}

function decodeTextBytes(bytes, cmap) {
  if (!cmap || !cmap.size) return decodePdfBytes(bytes);
  let out = '';
  let i = 0;
  while (i < bytes.length) {
    let matched = false;
    for (const width of [2, 1]) {
      if (i + width > bytes.length) continue;
      const key = bytes.slice(i, i + width).map((byte) => byte.toString(16).padStart(2, '0')).join('').toUpperCase();
      if (cmap.has(key)) { out += cmap.get(key); i += width; matched = true; break; }
    }
    if (!matched) {
      const byte = bytes[i++];
      if (byte >= 32 && byte !== 127) out += String.fromCharCode(byte);
    }
  }
  return out;
}

function tokenizeContent(input) {
  const tokens = [];
  let index = 0;
  const length = input.length;
  const isDelimiter = (char) => '()<>[]{}/%'.includes(char);
  const isWhitespace = (char) => char === ' ' || char === '\n' || char === '\r' || char === '\t' || char === '\f' || char === '\0';
  while (index < length) {
    const char = input[index];
    if (isWhitespace(char)) { index++; continue; }
    if (char === '%') { while (index < length && input[index] !== '\n' && input[index] !== '\r') index++; continue; }
    if (char === '(') {
      const parsed = pdfLiteralString(input, index);
      tokens.push({ type: 'string', value: decodePdfBytes(parsed.bytes), bytes: parsed.bytes });
      index = parsed.end;
      continue;
    }
    if (char === '<' && input[index + 1] !== '<') {
      const end = input.indexOf('>', index + 1);
      if (end < 0) break;
      const bytes = hexBytes(input.slice(index + 1, end));
      tokens.push({ type: 'string', value: decodePdfBytes(bytes), bytes });
      index = end + 1;
      continue;
    }
    if (char === '/') {
      let end = index + 1;
      while (end < length && !isWhitespace(input[end]) && !isDelimiter(input[end])) end++;
      tokens.push({ type: 'name', value: input.slice(index + 1, end) });
      index = end;
      continue;
    }
    if (char === '<' && input[index + 1] === '<') {
      index += 2;
      let depth = 1;
      while (index < length && depth > 0) {
        if (input[index] === '<' && input[index + 1] === '<') { depth++; index += 2; }
        else if (input[index] === '>' && input[index + 1] === '>') { depth--; index += 2; }
        else index++;
      }
      continue;
    }
    if (char === '[') {
      const array = [];
      index++;
      let nested = 0;
      while (index < length) {
        if (input[index] === '[') { nested++; index++; continue; }
        if (input[index] === ']') {
          if (nested === 0) { index++; break; }
          nested--; index++; continue;
        }
        const sub = tokenizeContent(input.slice(index, input.indexOf(']', index) + 1));
        array.push(...sub);
        index = input.indexOf(']', index) + 1;
        if (index <= 0) break;
      }
      tokens.push({ type: 'array', value: array });
      continue;
    }
    if (isDelimiter(char)) { index++; continue; }
    let end = index;
    while (end < length && !isWhitespace(input[end]) && !isDelimiter(input[end])) end++;
    const word = input.slice(index, end);
    if (/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(word)) tokens.push({ type: 'number', value: Number(word) });
    else tokens.push({ type: 'word', value: word });
    index = end;
  }
  return tokens;
}

function extractFromContent(content, fontMaps = {}) {
  const tokens = tokenizeContent(content);
  let output = '';
  let line = '';
  let lastY = null;
  let lastX = null;
  let currentMap = null;
  const stack = [];
  const newline = () => { if (line.trim()) output += line.trimEnd() + '\n'; line = ''; };
  const append = (value) => { if (value) line += value; };
  const textOf = (token) => {
    if (!token) return '';
    if (typeof token === 'string') return token;
    if (token.type === 'string') return decodeTextBytes(token.bytes || [], currentMap) || token.value || '';
    return '';
  };
  for (const token of tokens) {
    if (token.type === 'number' || token.type === 'string' || token.type === 'array' || token.type === 'name') {
      stack.push(token);
      continue;
    }
    if (token.type !== 'word') continue;
    const op = token.value;
    if (op === 'Td' || op === 'TD') {
      const ty = Number(stack[1] && stack[1].value) || 0;
      if (ty !== 0 && lastY !== null) newline();
      lastX = Number(stack[0] && stack[0].value) || 0; lastY = ty;
      stack.length = 0;
    } else if (op === 'Tm') {
      const x = Number(stack[4] && stack[4].value);
      const y = Number(stack[5] && stack[5].value);
      if (lastY !== null && y !== lastY) newline();
      lastX = x; lastY = y;
      stack.length = 0;
    } else if (op === 'T*') {
      newline(); stack.length = 0;
    } else if (op === 'Tj') {
      append(textOf(stack[stack.length - 1])); stack.length = 0;
    } else if (op === 'TJ') {
      const parts = stack[stack.length - 1] && Array.isArray(stack[stack.length - 1].value) ? stack[stack.length - 1].value : [];
      for (const part of parts) {
        if (part && part.type === 'string') append(textOf(part));
        else if (part && part.type === 'number' && part.value < -120 && line && !line.endsWith(' ')) append(' ');
      }
      stack.length = 0;
    } else if (op === "'") {
      newline(); append(textOf(stack[stack.length - 1])); stack.length = 0;
    } else if (op === '"') {
      newline(); append(textOf(stack[stack.length - 1])); stack.length = 0;
    } else if (op === 'Tf') {
      const font = stack[stack.length - 2];
      currentMap = font && font.type === 'name' ? (fontMaps[font.value] || null) : null;
      stack.length = 0;
    } else if (op === 'ET') {
      newline(); stack.length = 0;
    } else if (op === 'BT') {
      stack.length = 0;
    } else if (['Tc', 'Tw', 'Tz', 'TL', 'Ts', 'Tr', 'rg', 'RG', 'gs', 'q', 'Q', 'cm', 'w', 'J', 'j', 'M', 'd', 'i'].includes(op)) {
      stack.length = 0;
    } else if (op === 'Do' || op === 'BI') {
      stack.length = 0;
    } else if (op === 'ID') {
      // Inline image data is binary; stop this content stream rather than mis-tokenizing it.
      break;
    } else {
      stack.length = 0;
    }
  }
  newline();
  return output;
}

function parsePdf(buffer) {
  if (!Buffer.isBuffer(buffer)) buffer = Buffer.from(buffer);
  const latin = buffer.toString('latin1');
  if (!latin.slice(0, 1024).includes('%PDF-')) return { error: 'INVALID_PDF', message: 'missing %PDF header' };
  const objects = new Map();
  const objectRe = /(\d+)\s+(\d+)\s+obj\b/g;
  let match;
  while ((match = objectRe.exec(latin))) {
    const num = Number(match[1]);
    const start = objectRe.lastIndex;
    const end = latin.indexOf('endobj', start);
    if (end < 0) continue;
    const body = latin.slice(start, end);
    const streamMarker = body.match(/\bstream\r?\n/);
    let dict = body;
    let stream = null;
    if (streamMarker) {
      const dictEnd = streamMarker.index;
      dict = body.slice(0, dictEnd);
      const dataStart = start + dictEnd + streamMarker[0].length;
      const dataEnd = latin.indexOf('endstream', dataStart);
      if (dataEnd >= 0) stream = buffer.subarray(dataStart, dataEnd);
    }
    objects.set(num, { num, dict, stream, body });
  }

  // Expand compressed object streams (common in modern PDFs).
  for (const object of [...objects.values()]) {
    if (!object.stream || !/\/Type\s*\/ObjStm/.test(object.dict)) continue;
    const decoded = decodeStream(object.stream, object.dict);
    if (!decoded) continue;
    const count = Number((object.dict.match(/\/N\s+(\d+)/) || [])[1]) || 0;
    const first = Number((object.dict.match(/\/First\s+(\d+)/) || [])[1]) || 0;
    const header = decoded.subarray(0, first).toString('latin1').trim().split(/\s+/).map(Number);
    for (let i = 0; i < count; i++) {
      const num = header[i * 2];
      const offset = header[i * 2 + 1];
      if (!Number.isFinite(num) || !Number.isFinite(offset)) continue;
      if (objects.has(num)) continue;
      const nextOffset = i + 1 < count ? first + header[(i + 1) * 2 + 1] : decoded.length;
      const body = decoded.subarray(first + offset, nextOffset).toString('latin1');
      objects.set(num, { num, dict: body, stream: null, body });
    }
  }

  if (/\/Encrypt\b/.test(latin)) return { error: 'ENCRYPTED_PDF', encrypted: true, message: 'encrypted PDFs are not supported natively' };

  const refs = (dict, key) => {
    const direct = dict.match(new RegExp(`${key}\\s*\\[([^\\]]*)\\]`));
    if (direct) return [...direct[1].matchAll(/(\d+)\s+\d+\s+R/g)].map((m) => Number(m[1]));
    const single = dict.match(new RegExp(`${key}\\s+(\\d+)\\s+\\d+\\s+R`));
    return single ? [Number(single[1])] : [];
  };
  const findType = (type) => [...objects.values()].find((object) => new RegExp(`/Type\\s*/${type}\\b`).test(object.dict));
  const decodeObject = (num) => {
    const object = objects.get(num);
    if (!object || !object.stream) return null;
    return decodeStream(object.stream, object.dict);
  };

  const pages = [];
  const catalog = findType('Catalog');
  const rootPages = catalog ? refs(catalog.dict, '/Pages')[0] : null;
  const visited = new Set();
  const walk = (num, depth = 0) => {
    if (!num || visited.has(num) || depth > 50) return;
    visited.add(num);
    const object = objects.get(num);
    if (!object) return;
    if (/\/Type\s*\/Page\b/.test(object.dict)) {
      const fontMaps = {};
      const resourcesRef = refs(object.dict, '/Resources')[0];
      const resources = resourcesRef ? objects.get(resourcesRef) : null;
      const resourceDict = resources ? resources.dict : object.dict;
      const fontRef = (resourceDict.match(/\/Font\s+(\d+)\s+\d+\s+R/) || [])[1];
      const fontDictionaries = [];
      if (fontRef) {
        const fontObject = objects.get(Number(fontRef));
        if (fontObject) fontDictionaries.push(fontObject.dict);
      } else {
        const inlineFont = resourceDict.match(/\/Font\s*<<([\s\S]*?)>>/);
        if (inlineFont) fontDictionaries.push(inlineFont[1]);
      }
      for (const fontDict of fontDictionaries) {
        for (const font of fontDict.matchAll(/\/([A-Za-z0-9]+)\s+(\d+)\s+\d+\s+R/g)) {
          const target = objects.get(Number(font[2]));
          const unicodeRef = target ? (target.dict.match(/\/ToUnicode\s+(\d+)\s+\d+\s+R/) || [])[1] : null;
          if (!unicodeRef) continue;
          const unicodeObject = objects.get(Number(unicodeRef));
          const cmapStream = unicodeObject ? decodeStream(unicodeObject.stream, unicodeObject.dict) : null;
          if (cmapStream) fontMaps[font[1]] = parseCMap(cmapStream);
        }
      }
      const contents = refs(object.dict, '/Contents');
      const text = contents.map((ref) => {
        const data = decodeObject(ref);
        return data ? extractFromContent(data.toString('latin1'), fontMaps) : '';
      }).join('\n');
      pages.push({ index: pages.length, text: text.replace(/\n{3,}/g, '\n\n').trim() });
      return;
    }
    for (const kid of refs(object.dict, '/Kids')) walk(kid, depth + 1);
  };
  if (rootPages) walk(rootPages);

  if (!pages.length) {
    // Fallback for malformed page trees: scan content-looking streams in object order.
    const candidates = [...objects.values()]
      .filter((object) => object.stream)
      .sort((a, b) => a.num - b.num);
    for (const object of candidates) {
      const data = decodeStream(object.stream, object.dict);
      if (!data) continue;
      const content = data.toString('latin1');
      if (!/\bBT\b/.test(content) || !/(?:Tj|TJ)\b/.test(content)) continue;
      const text = extractFromContent(content);
      if (text.trim()) pages.push({ index: pages.length, text: text.replace(/\n{3,}/g, '\n\n').trim() });
    }
  }

  let title = '';
  const infoRef = (latin.match(/\/Info\s+(\d+)\s+\d+\s+R/) || [])[1];
  const info = infoRef ? objects.get(Number(infoRef)) : null;
  const titleObject = (info && /\/Title\s*(?:\(|<)/.test(info.dict))
    ? info
    : [...objects.values()].find((object) => /\/Title\s*(?:\(|<)/.test(object.dict));
  if (titleObject) {
    const hexTitle = titleObject.dict.match(/\/Title\s*<([0-9A-Fa-f\s]+)>/);
    if (hexTitle) {
      title = decodePdfBytes(hexBytes(hexTitle[1])).replace(/\s+/g, ' ').trim();
    } else {
      const titleMatch = titleObject.dict.match(/\/Title\s*\(([\s\S]*?)\)/);
      if (titleMatch) {
        const parsed = pdfLiteralString(`(${titleMatch[1]})`, 0);
        title = decodePdfBytes(parsed.bytes).replace(/\s+/g, ' ').trim();
      }
    }
  }
  const text = pages.map((page) => page.text).filter(Boolean).join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
  return {
    text,
    pages,
    pageCount: pages.length,
    title,
    encrypted: false,
    needsOcr: !text || text.replace(/\s+/g, '').length === 0,
  };
}

function extractPdfText(buffer) {
  try {
    return parsePdf(buffer);
  } catch (error) {
    return { error: 'PDF_PARSE_FAILED', message: String(error.message || error), text: '', pages: [], pageCount: 0, needsOcr: false };
  }
}

module.exports = { extractPdfText, extractFromContent, tokenizeContent, decodeStream };
