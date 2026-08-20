#!/usr/bin/env node
/**
 * PDF generation CLI for OpenClaw — produces spec-compliant PDFs via pdfkit.
 *
 * Exists because a hand-crafted PDF (raw bytes assembled without a real PDF
 * library) previously made it into the ingestion pipeline and broke
 * ingest-cli's PDF extraction with "Invalid number: (charCode 32)" —
 * whitespace where the PDF structure required a numeric value. pdfkit writes
 * well-formed PDF objects/xref tables itself, so anything produced here
 * round-trips through ingest-cli cleanly. (ingest-cli's extractor itself was
 * separately swapped from pdf-parse to unpdf — see ingest.js — after this
 * investigation also turned up a second, unrelated bug: pdf-parse's
 * hardcoded 2018-era pdfjs-dist broke on *any* PDF, malformed or not, once
 * Node's 'http' module was loaded anywhere in the process.)
 *
 * Input is plain text or light Markdown (headings, bullet lists, blank-line
 * paragraph breaks) — not full CommonMark. pdfkit handles line wrapping and
 * pagination automatically.
 *
 * Usage
 * -----
 *   pdf-cli notes.md --output notes.pdf --title "My Doc"
 *   echo "some text" | pdf-cli --stdin --output out.pdf --title "Quick Note"
 *   pdf-cli --input notes.txt --output out.pdf
 *
 * All output is JSON on stdout. Errors are JSON on stderr + exit 1.
 */

'use strict';

const fs   = require('fs');
const path = require('path');
const PDFDocument = require('pdfkit');

// ── CLI args ─────────────────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = { flags: {}, positional: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      args.flags[key] = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
    } else {
      args.positional.push(a);
    }
  }
  return args;
}

const { flags, positional } = parseArgs(process.argv.slice(2));
const inputPath  = flags.input || positional[0] || null;
const useStdin   = !!flags.stdin;
const outputPath = flags.output || null;
const title      = flags.title || (inputPath ? path.basename(inputPath, path.extname(inputPath)) : 'Untitled');
const pageSize   = flags.pageSize || 'LETTER';

// ── Minimal Markdown-ish layout ─────────────────────────────────────────────
// Not a CommonMark parser — just the handful of conventions a plain-text or
// lightly-formatted transcript/summary is likely to use.

function renderBody(doc, text) {
  const lines = text.split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { doc.moveDown(0.5); i++; continue; }

    const h1 = /^#\s+(.*)/.exec(line);
    const h2 = /^##\s+(.*)/.exec(line);
    const bullet = /^[-*]\s+(.*)/.exec(line);

    if (h1) {
      doc.moveDown(0.5).fontSize(16).font('Helvetica-Bold').text(h1[1]);
      doc.font('Helvetica').fontSize(11);
    } else if (h2) {
      doc.moveDown(0.5).fontSize(13).font('Helvetica-Bold').text(h2[1]);
      doc.font('Helvetica').fontSize(11);
    } else if (bullet) {
      doc.text(`•  ${bullet[1]}`, { indent: 16 });
    } else {
      doc.text(line);
    }
    i++;
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────

(async () => {
  try {
    if (!inputPath && !useStdin) {
      const help = [
        'Usage: pdf-cli <file> --output <path.pdf> [options]',
        '       echo "text" | pdf-cli --stdin --output <path.pdf> --title "My Doc"',
        '',
        'Options:',
        '  --output <path>     Output PDF path (required)',
        '  --title <str>       Document title, shown as a header and in PDF metadata',
        '                      (default: input filename, or "Untitled" for stdin)',
        '  --input <path>      Read from a file instead of a positional arg',
        '  --stdin             Read from stdin instead of a file',
        '  --page-size <str>   LETTER (default) | A4 | LEGAL',
        '',
        'Input is plain text or light Markdown: "# " / "## " headings,',
        '"- "/"* " bullet lists, blank lines as paragraph breaks.',
      ];
      process.stderr.write(JSON.stringify({ error: 'No input specified', usage: help }) + '\n');
      process.exit(1);
    }
    if (!outputPath) {
      process.stderr.write(JSON.stringify({ error: '--output <path.pdf> is required' }) + '\n');
      process.exit(1);
    }

    let text;
    if (useStdin) {
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      text = Buffer.concat(chunks).toString('utf8');
    } else {
      text = fs.readFileSync(inputPath, 'utf8');
    }
    if (!text.trim()) {
      process.stderr.write(JSON.stringify({ error: 'No text content to render' }) + '\n');
      process.exit(1);
    }

    const doc = new PDFDocument({
      size: pageSize,
      margin: 54,
      info: { Title: title, Producer: 'openclaw pdf-cli (pdfkit)' },
    });

    let pageCount = 1; // the constructor creates page 1 without firing 'pageAdded'
    doc.on('pageAdded', () => pageCount++);

    const outStream = fs.createWriteStream(outputPath);
    const finished = new Promise((resolve, reject) => {
      outStream.on('finish', resolve);
      outStream.on('error', reject);
      doc.on('error', reject);
    });
    doc.pipe(outStream);

    doc.font('Helvetica-Bold').fontSize(20).text(title);
    doc.moveDown();
    doc.font('Helvetica').fontSize(11);
    renderBody(doc, text);

    doc.end();
    await finished;

    const stat = fs.statSync(outputPath);

    process.stdout.write(JSON.stringify({
      status: 'ok',
      output: path.resolve(outputPath),
      title,
      pages: pageCount,
      bytes: stat.size,
    }, null, 2) + '\n');
  } catch (err) {
    process.stderr.write(JSON.stringify({ error: err.message }) + '\n');
    process.exit(1);
  }
})();
