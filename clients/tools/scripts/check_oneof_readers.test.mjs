import assert from 'node:assert/strict';
import test from 'node:test';
import {readerDiagnostics} from './check_oneof_readers.mjs';

test('oneof readers require a never proof even with a silent default', () => {
  for (const tail of ['', 'default: break;', 'default: other satisfies never;']) {
    const errors = readerDiagnostics(
      'reader.ts',
      `switch (body.case) { case 'event': break; ${tail} }`,
    );
    assert.equal(errors.length, 1);
    assert.match(errors[0], /reader.ts:1:.*satisfies never/);
  }
  assert.deepEqual(
    readerDiagnostics(
      'reader.ts',
      "switch (body.case) { case 'event': break; default: body satisfies never; }",
    ),
    [],
  );
});

test('all comparison dispatch spellings need an exhaustive switch', () => {
  for (const source of [
    "if (body.case === 'bytes') {} else if (body.case === 'end') {}",
    "body.case === 'event' ? event() : search()",
    "const matches = 'event' === body['case'];",
    "const matches = body.case !== 'event';",
    "const matches = (body.case) === ('event');",
    "const EVENT = 'event'; if (body.case === EVENT) {}",
    "const kind = body.case; if (kind === 'event') {}",
    "const {case: kind} = body; kind === 'event' ? event() : search();",
  ])
    assert.match(readerDiagnostics('reader.ts', source)[0], /reader.ts:1:.*exhaustive switch/);
});

test('computed case access has the same guard and unrelated switches remain free', () => {
  for (const access of ["body['case']", 'body["case"]']) {
    assert.equal(
      readerDiagnostics('reader.ts', `switch (${access}) { default: break; }`).length,
      1,
    );
    assert.deepEqual(
      readerDiagnostics('reader.ts', `switch (${access}) { default: body satisfies never; }`),
      [],
    );
  }
  assert.deepEqual(readerDiagnostics('reader.ts', 'switch (method) { default: break; }'), []);
  assert.deepEqual(readerDiagnostics('writer.ts', "body.case = 'event';"), []);
  assert.deepEqual(
    readerDiagnostics(
      'reader.ts',
      "const kind = body.case; switch (kind) {case 'event': break; default: body satisfies never;}",
    ),
    [],
  );
});

test('oneofIs rejects a new kind until the caller makes an explicit decision', async () => {
  const {readFileSync} = await import('node:fs');
  const ts = (await import('typescript')).default;
  const helper = readFileSync(
    new URL('../../libs/orchestrator-proto/src/oneof.ts', import.meta.url),
    'utf8',
  );
  const compile = kinds => {
    const name = '/oneof-fixture.ts';
    const source = `${helper}\n type Body = {case: 'event'; value: number} | {case: 'search'; value: string};
      function read(body: Body) { return oneofIs(body, 'event', ${kinds}); }
      function renamed(body: Body) { const {case: kind} = body; const EVENT = 'event'; return kind === EVENT; }`;
    const options = {strict: true, noEmit: true, types: [], target: ts.ScriptTarget.ESNext};
    const host = ts.createCompilerHost(options);
    const original = host.getSourceFile.bind(host);
    host.getSourceFile = (file, version) =>
      file === name ? ts.createSourceFile(file, source, version, true) : original(file, version);
    const program = ts.createProgram([name], options, host);
    const typed = readerDiagnostics(name, source, {
      file: program.getSourceFile(name),
      checker: program.getTypeChecker(),
    });
    // The generic helper compares an open string contract; the local alias is a closed oneof.
    assert.equal(typed.length, 1);
    assert.match(typed[0], /oneof-fixture.ts:.*exhaustive switch/);
    return ts
      .getPreEmitDiagnostics(program)
      .map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n'));
  };
  assert.deepEqual(compile('{event: true, search: true}'), []);
  assert.match(compile('{event: true}').join('\n'), /search.*missing/);
});

test('Vitest fails a test whose respond throw was caught by its caller', async () => {
  const {mkdtempSync, writeFileSync, rmSync} = await import('node:fs');
  const {fileURLToPath} = await import('node:url');
  const {join} = await import('node:path');
  const {spawnSync} = await import('node:child_process');
  const desktop = fileURLToPath(new URL('../../apps/desktop/', import.meta.url));
  const directory = mkdtempSync(join(desktop, 'src/testing/callback-fixture-'));
  try {
    const fixture = join(directory, 'swallowed.test.ts');
    writeFileSync(
      fixture,
      `
      import {it} from '@effect/vitest';
      import * as Effect from 'effect/Effect';
      import * as Stream from 'effect/Stream';
      import {GatewayTransport} from '../../core/transport/gateway-transport.js';
      import {makeFakeTransport} from '../transport/fake-transport.js';
      it.effect('caught callback failure', () => Effect.gen(function* () {
        const fake = makeFakeTransport({respond() { throw new Error('PLANTED-RESPOND-FAILURE'); }});
        yield* Effect.gen(function* () {
          const transport = yield* GatewayTransport;
          yield* Stream.runDrain(transport.stream({host: 'local', method: '/test', request: new Uint8Array()}, 1));
        }).pipe(Effect.provide(fake.layer), Effect.exit);
      }));
    `,
    );
    const result = spawnSync(
      process.execPath,
      [fileURLToPath(new URL('./run_vitest.mjs', import.meta.url)), fixture],
      {cwd: desktop, encoding: 'utf8', timeout: 120_000},
    );
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.match(result.stdout + result.stderr, /PLANTED-RESPOND-FAILURE/);
    assert.match(result.stdout + result.stderr, /1 failed/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});
