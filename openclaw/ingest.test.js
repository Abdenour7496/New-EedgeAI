/**
 * Pure-logic tests for ingest.js — chunking, id derivation, and the
 * idempotency match rule. Deliberately does not touch MinIO or Graphiti
 * (those are exercised by manual end-to-end verification, documented in
 * docs/adr/0021-openclaw-ingest-cli-hardening.md) — this covers the parts
 * that can silently regress without any network call ever failing.
 *
 * Run with: node --test ingest.test.js
 * (Node's built-in test runner — no new dependency needed, matching the
 * rest of this stack's "mocked unit tests need nothing extra installed"
 * pattern in proxy/tests and openwebui-functions/tests.)
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  parseArgs, chunkText, md5Uuid, computeDocumentId, episodeMatchesDocumentId,
} = require('./ingest.js');

test('parseArgs', async (t) => {
  await t.test('splits flags and positional args', () => {
    const { flags, positional } = parseArgs(['file.pdf', '--title', 'My Doc', '--collection', 'docs']);
    assert.deepEqual(positional, ['file.pdf']);
    assert.equal(flags.title, 'My Doc');
    assert.equal(flags.collection, 'docs');
  });

  await t.test('a boolean flag with no value becomes true, not the next flag', () => {
    const { flags } = parseArgs(['--stdin', '--title', 'x']);
    assert.equal(flags.stdin, true);
    assert.equal(flags.title, 'x');
  });

  await t.test('kebab-case flags become camelCase', () => {
    const { flags } = parseArgs(['--agent-id', 'a1', '--chunk-size', '500', '--no-vision']);
    assert.equal(flags.agentId, 'a1');
    assert.equal(flags.chunkSize, '500');
    assert.equal(flags.noVision, true);
  });
});

test('chunkText', async (t) => {
  await t.test('short text under the size limit is a single chunk', () => {
    const chunks = chunkText('hello world', 2000, 200);
    assert.deepEqual(chunks, ['hello world']);
  });

  await t.test('empty text produces no chunks', () => {
    assert.deepEqual(chunkText('', 2000, 200), []);
  });

  await t.test('long text is split into more than one chunk', () => {
    const text = 'word '.repeat(1000); // 5000 chars
    const chunks = chunkText(text, 2000, 200);
    assert.ok(chunks.length > 1, `expected multiple chunks, got ${chunks.length}`);
    for (const c of chunks) assert.ok(c.length > 0, 'no chunk should be empty');
  });

  await t.test('reassembling chunks covers the original content (allowing for overlap)', () => {
    const text = Array.from({ length: 50 }, (_, i) => `Sentence number ${i}.`).join(' ');
    const chunks = chunkText(text, 100, 20);
    const joined = chunks.join(' ');
    // Every distinct word from the source should appear somewhere in the
    // chunked output — a coarse but effective check that chunking doesn't
    // silently drop content.
    for (const word of text.split(/\s+/)) {
      if (!word) continue;
      assert.ok(joined.includes(word), `lost "${word}" while chunking`);
    }
  });

  await t.test('prefers breaking at a word boundary over the raw size limit', () => {
    // size=55 would otherwise cut mid-run at index 55 (five 'b's into the
    // second word); the nearby space at index 50 should win instead, so
    // the first chunk is exactly the first word with no 'b's in it.
    const text = 'a'.repeat(50) + ' ' + 'b'.repeat(50);
    const chunks = chunkText(text, 55, 5);
    assert.equal(chunks[0], 'a'.repeat(50));
    assert.ok(!chunks[0].includes('b'), 'first chunk should not spill into the second word');
  });
});

test('md5Uuid', async (t) => {
  await t.test('is deterministic for the same input', () => {
    assert.equal(md5Uuid('same-input'), md5Uuid('same-input'));
  });

  await t.test('differs for different input', () => {
    assert.notEqual(md5Uuid('input-a'), md5Uuid('input-b'));
  });

  await t.test('has the standard 8-4-4-4-12 UUID shape', () => {
    const id = md5Uuid('anything');
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });
});

test('computeDocumentId — the core of docs/adr/0021\'s idempotency fix', async (t) => {
  await t.test('identical content/collection/title always compute the same id', () => {
    const buf = Buffer.from('identical file bytes');
    const id1 = computeDocumentId(buf, 'documents', 'My Doc');
    const id2 = computeDocumentId(buf, 'documents', 'My Doc');
    assert.equal(id1, id2, 'a retry of the same ingest must compute the same document id');
  });

  await t.test('does NOT depend on wall-clock time', () => {
    // Regression guard for the exact bug this ADR fixed: the old
    // implementation hashed in Date.now(), so two calls a moment apart
    // always produced different ids. Calling this twice in a tight loop
    // (guaranteed different Date.now() values across the two calls) must
    // still produce the same id.
    const buf = Buffer.from('time-independence check');
    const before = Date.now();
    const id1 = computeDocumentId(buf, 'documents', 'title');
    while (Date.now() === before) { /* burn at least 1ms so real time moves */ }
    const id2 = computeDocumentId(buf, 'documents', 'title');
    assert.equal(id1, id2);
  });

  await t.test('different content produces a different id', () => {
    const idA = computeDocumentId(Buffer.from('content A'), 'documents', 'title');
    const idB = computeDocumentId(Buffer.from('content B'), 'documents', 'title');
    assert.notEqual(idA, idB);
  });

  await t.test('different collection produces a different id (same content/title)', () => {
    const buf = Buffer.from('same content');
    const idA = computeDocumentId(buf, 'collectionA', 'title');
    const idB = computeDocumentId(buf, 'collectionB', 'title');
    assert.notEqual(idA, idB);
  });

  await t.test('different title produces a different id (same content/collection)', () => {
    const buf = Buffer.from('same content');
    const idA = computeDocumentId(buf, 'documents', 'Title A');
    const idB = computeDocumentId(buf, 'documents', 'Title B');
    assert.notEqual(idA, idB);
  });

  await t.test('is a 16-char lowercase hex string (matches the storage-key convention)', () => {
    const id = computeDocumentId(Buffer.from('x'), 'documents', 'title');
    assert.match(id, /^[0-9a-f]{16}$/);
  });
});

test('episodeMatchesDocumentId', async (t) => {
  await t.test('matches when the episode content contains the exact "Document ID: <id>" marker', () => {
    const episode = { content: 'Document: X\nDocument ID: abc123\n\nbody text' };
    assert.equal(episodeMatchesDocumentId(episode, 'abc123'), true);
  });

  await t.test('does not match a different document id', () => {
    const episode = { content: 'Document ID: abc123\n\nbody' };
    assert.equal(episodeMatchesDocumentId(episode, 'zzz999'), false);
  });

  await t.test('matches regardless of where in the content the marker line sits', () => {
    const episode = { content: 'Document: X\nSource: y\nDocument ID: abc123\nChunk: 1/3\n\nbody' };
    assert.equal(episodeMatchesDocumentId(episode, 'abc123'), true);
  });

  await t.test('handles episodes with no content field without throwing', () => {
    assert.equal(episodeMatchesDocumentId({}, 'abc123'), false);
    assert.equal(episodeMatchesDocumentId({ content: null }, 'abc123'), false);
    assert.equal(episodeMatchesDocumentId(null, 'abc123'), false);
  });
});
