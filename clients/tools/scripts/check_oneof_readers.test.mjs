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

test('respond assertions must be made by the test after collecting observations', () => {
  for (const source of [
    '({respond: () => { expect(value).toBe(1); }})',
    '({respond() { expect(value).toBe(1); }})',
  ]) {
    assert.match(
      readerDiagnostics('reader.test.ts', source)[0],
      /reader.test.ts:1:.*record observations/,
    );
  }
  assert.deepEqual(
    readerDiagnostics(
      'reader.test.ts',
      '({respond: () => { observations.push(value); }}); expect(observations).toEqual([1]);',
    ),
    [],
  );
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
});
