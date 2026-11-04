// Native Office text extraction: DOCX, XLSX, PPTX.
// Parses the OOXML zip container directly (central directory + inflateRaw) and
// extracts text from the XML parts. No Office library, no framework, no dependency.
'use strict';

const zlib = require('zlib');

function readUInt16(buffer, offset) { return buffer.readUInt16LE(offset); }
function readUInt32(buffer, offset) { return buffer.readUInt32LE(offset); }

function unzip(buffer) {
  if (!Buffer.isBuffer(buffer)) buffer = Buffer.from(buffer);
  let eocd = -1;
  const min = Math.max(0, buffer.length - 66000);
  for (let i = buffer.length - 22; i >= min; i--) {
    if (readUInt32(buffer, i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('not a zip archive');
  const count = readUInt16(buffer, eocd + 10);
  const central = readUInt32(buffer, eocd + 16);
  const files = new Map();
  let pointer = central;
  for (let index = 0; index < count; index++) {
    if (pointer + 46 > buffer.length || readUInt32(buffer, pointer) !== 0x02014b50) break;
    const method = readUInt16(buffer, pointer + 10);
    const compressedSize = readUInt32(buffer, pointer + 20);
    const nameLength = readUInt16(buffer, pointer + 28);
    const extraLength = readUInt16(buffer, pointer + 30);
    const commentLength = readUInt16(buffer, pointer + 32);
    const localOffset = readUInt32(buffer, pointer + 42);
    const name = buffer.subarray(pointer + 46, pointer + 46 + nameLength).toString('utf8').replace(/\\/g, '/');
    pointer += 46 + nameLength + extraLength + commentLength;
    if (localOffset + 30 > buffer.length || readUInt32(buffer, localOffset) !== 0x04034b50) continue;
    const localNameLength = readUInt16(buffer, localOffset + 26);
    const localExtraLength = readUInt16(buffer, localOffset + 28);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    const raw = buffer.subarray(dataStart, dataStart + compressedSize);
    let data;
    if (method === 0) data = raw;
    else if (method === 8) data = zlib.inflateRawSync(raw);
    else continue;
    files.set(name, data);
  }
  return files;
}

function decodeXml(value) {
  return String(value || '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&apos;/gi, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCharCode(parseInt(code, 16)));
}

function xmlToText(xml) {
  return decodeXml(String(xml || '')
    .replace(/<w:tab\b[^>]*\/>/g, '\t')
    .replace(/<w:br\b[^>]*\/>/g, '\n')
    .replace(/<\/w:tc>/g, '\t')
    .replace(/<\/w:tr>/g, '\n')
    .replace(/<\/a:tc>/g, '\t')
    .replace(/<\/a:tr>/g, '\n')
    .replace(/<\/w:p>/g, '\n')
    .replace(/<\/a:p>/g, '\n')
    .replace(/<\/text:p>/g, '\n')
    .replace(/<[^>]+>/g, ''))
    .replace(/\n\t/g, '\t')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function coreTitle(files) {
  const core = files.get('docProps/core.xml');
  if (!core) return '';
  const match = core.toString('utf8').match(/<dc:title[^>]*>([\s\S]*?)<\/dc:title>/i);
  return match ? decodeXml(match[1]).replace(/\s+/g, ' ').trim() : '';
}

function extractDocx(files) {
  const parts = [...files.entries()]
    .filter(([name]) => /^word\/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml$/.test(name))
    .sort(([a], [b]) => (a === 'word/document.xml' ? -1 : b === 'word/document.xml' ? 1 : a.localeCompare(b)));
  const text = parts.map(([, data]) => xmlToText(data.toString('utf8'))).filter(Boolean).join('\n\n');
  return { text, title: coreTitle(files), kind: 'docx' };
}

function sharedStrings(files) {
  const xml = files.get('xl/sharedStrings.xml');
  if (!xml) return [];
  const text = xml.toString('utf8');
  return [...text.matchAll(/<si\b[^>]*>([\s\S]*?)<\/si>/g)].map((match) => {
    return [...match[1].matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)].map((part) => decodeXml(part[1])).join('');
  });
}

function extractXlsx(files) {
  const strings = sharedStrings(files);
  const sheets = [...files.entries()]
    .filter(([name]) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name))
    .sort(([a], [b]) => Number(a.match(/(\d+)/)[1]) - Number(b.match(/(\d+)/)[1]));
  const output = [];
  for (const [name, data] of sheets) {
    const xml = data.toString('utf8');
    const rows = [];
    for (const row of xml.matchAll(/<row\b[^>]*>([\s\S]*?)<\/row>/g)) {
      const values = [];
      for (const cell of row[1].matchAll(/<c\b([^>]*)>([\s\S]*?)<\/c>/g)) {
        const attrs = cell[1];
        const inner = cell[2];
        const type = (attrs.match(/\bt="([^"]+)"/) || [])[1];
        let value = '';
        if (type === 's') {
          const index = Number((inner.match(/<v>([\s\S]*?)<\/v>/) || [])[1]);
          value = strings[index] == null ? '' : strings[index];
        } else if (type === 'inlineStr') {
          value = [...inner.matchAll(/<t\b[^>]*>([\s\S]*?)<\/t>/g)].map((part) => decodeXml(part[1])).join('');
        } else {
          value = decodeXml((inner.match(/<v>([\s\S]*?)<\/v>/) || [])[1] || '');
        }
        values.push(value);
      }
      if (values.some((value) => value !== '')) rows.push(values.join('\t'));
    }
    if (rows.length) output.push(rows.join('\n'));
  }
  return { text: output.join('\n\n'), title: coreTitle(files), kind: 'xlsx' };
}

function extractPptx(files) {
  const slides = [...files.keys()]
    .filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name))
    .sort((a, b) => Number(a.match(/(\d+)\.xml$/)[1]) - Number(b.match(/(\d+)\.xml$/)[1]));
  const output = [];
  for (const name of slides) {
    const xml = files.get(name).toString('utf8');
    const text = [...xml.matchAll(/<a:t\b[^>]*>([\s\S]*?)<\/a:t>/g)].map((match) => decodeXml(match[1])).join('\n');
    if (text.trim()) output.push(`Slide ${output.length + 1}\n${text.trim()}`);
  }
  return { text: output.join('\n\n'), title: coreTitle(files), kind: 'pptx' };
}

function extractOfficeText(buffer, { ext = '' } = {}) {
  try {
    const files = unzip(buffer);
    const extension = String(ext || '').toLowerCase();
    if (extension === '.docx') return extractDocx(files);
    if (extension === '.xlsx') return extractXlsx(files);
    if (extension === '.pptx') return extractPptx(files);
    if (files.has('word/document.xml')) return extractDocx(files);
    if (files.has('xl/workbook.xml')) return extractXlsx(files);
    if (files.has('ppt/presentation.xml')) return extractPptx(files);
    return { error: 'UNSUPPORTED_OFFICE', message: 'not a DOCX/XLSX/PPTX container' };
  } catch (error) {
    return { error: 'OFFICE_PARSE_FAILED', message: String(error.message || error) };
  }
}

module.exports = { extractOfficeText, unzip, decodeXml };
