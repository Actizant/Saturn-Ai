// Local OCR adapters. OCR needs an engine; this module keeps the integration native
// and dependency-free by calling a locally installed Tesseract binary (or any hook).
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

function ocrError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function tesseractOcr({ command = 'tesseract', language = 'eng', psm = 6, timeoutMs = 60000, dpi = null, config = [], preprocess = null } = {}) {
  return async (buffer, { contentType = '' } = {}) => {
    let input = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer);
    if (!input.length) throw ocrError('OCR_EMPTY_INPUT', 'empty image');
    if (typeof preprocess === 'function') input = await preprocess(input, { contentType });
    const args = ['stdin', 'stdout', '-l', language, '--psm', String(psm)];
    if (dpi) args.push('--dpi', String(Math.max(70, Number(dpi) || 300)));
    for (const option of Array.isArray(config) ? config : []) args.push(String(option));
    const result = spawnSync(command, args, {
      input,
      encoding: 'utf8',
      timeout: timeoutMs,
      maxBuffer: 16 * 1024 * 1024,
      windowsHide: true,
    });
    if (result.error) {
      if (result.error.code === 'ENOENT') throw ocrError('OCR_ENGINE_UNAVAILABLE', `Tesseract not found: ${command}`);
      throw ocrError('OCR_FAILED', String(result.error.message || result.error));
    }
    if (result.status !== 0) {
      throw ocrError('OCR_FAILED', String(result.stderr || `Tesseract exited ${result.status}`).trim());
    }
    return String(result.stdout || '').replace(/\f/g, '\n').trim();
  };
}

function imageMime(file = '') {
  const ext = String(file).toLowerCase().split('.').pop();
  if (ext === 'png') return 'image/png';
  if (ext === 'jpg' || ext === 'jpeg') return 'image/jpeg';
  if (ext === 'webp') return 'image/webp';
  if (ext === 'tif' || ext === 'tiff') return 'image/tiff';
  if (ext === 'bmp') return 'image/bmp';
  return '';
}

function popplerRenderer({ command = 'pdftoppm', dpi = 300, timeoutMs = 120000, grayscale = false, format = 'png' } = {}) {
  return async (buffer, { password = '' } = {}) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'um-pdf-pages-'));
    const input = path.join(dir, 'input.pdf');
    const prefix = path.join(dir, 'page');
    const imageFormat = ['png', 'jpeg', 'tiff'].includes(format) ? format : 'png';
    try {
      fs.writeFileSync(input, Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer));
      const args = [`-${imageFormat}`, '-r', String(dpi)];
      if (grayscale) args.push('-gray');
      if (password) args.push('-upw', String(password));
      args.push(input, prefix);
      const result = spawnSync(command, args, {
        encoding: 'utf8',
        timeout: timeoutMs,
        maxBuffer: 16 * 1024 * 1024,
        windowsHide: true,
      });
      if (result.error) {
        if (result.error.code === 'ENOENT') throw ocrError('PDF_RENDERER_UNAVAILABLE', `Poppler not found: ${command}`);
        throw ocrError('PDF_RENDER_FAILED', String(result.error.message || result.error));
      }
      if (result.status !== 0) throw ocrError('PDF_RENDER_FAILED', String(result.stderr || `pdftoppm exited ${result.status}`).trim());
      const pages = fs.readdirSync(dir)
        .filter((name) => /^page-\d+\.(?:png|jpg|jpeg|tiff)$/i.test(name))
        .sort((a, b) => Number(a.match(/(\d+)/)[1]) - Number(b.match(/(\d+)/)[1]))
        .map((name) => fs.readFileSync(path.join(dir, name)));
      if (!pages.length) throw ocrError('PDF_RENDER_EMPTY', 'pdftoppm produced no page images');
      return pages;
    } finally {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
    }
  };
}

function popplerTextExtractor({ command = 'pdftotext', layout = true, timeoutMs = 120000 } = {}) {
  return async (buffer, { password = '' } = {}) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'um-pdf-text-'));
    const input = path.join(dir, 'input.pdf');
    try {
      fs.writeFileSync(input, Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer));
      const args = [];
      if (layout) args.push('-layout');
      if (password) args.push('-upw', String(password));
      args.push(input, '-');
      const result = spawnSync(command, args, {
        encoding: 'utf8',
        timeout: timeoutMs,
        maxBuffer: 32 * 1024 * 1024,
        windowsHide: true,
      });
      if (result.error) {
        if (result.error.code === 'ENOENT') throw ocrError('PDF_TEXT_UNAVAILABLE', `pdftotext not found: ${command}`);
        throw ocrError('PDF_TEXT_FAILED', String(result.error.message || result.error));
      }
      if (result.status !== 0) {
        const message = String(result.stderr || `pdftotext exited ${result.status}`).trim();
        if (/password|encrypt/i.test(message)) throw ocrError('PDF_PASSWORD_REQUIRED', message);
        throw ocrError('PDF_TEXT_FAILED', message);
      }
      return String(result.stdout || '').replace(/\f/g, '\n').trim();
    } finally {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
    }
  };
}

module.exports = { tesseractOcr, popplerRenderer, popplerTextExtractor, imageMime };
