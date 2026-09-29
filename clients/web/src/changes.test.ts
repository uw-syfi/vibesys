import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {DesignRound} from '@vibesys/backend-client';
import {
  changesModel,
  loadPatchSlot,
  type PatchSlot,
  parsePatch,
  patchKey,
  patchRequests,
  shellQuote,
} from './changes.js';
import type {RoundRow} from './rounds.js';

const DESIGN: DesignRound = {
  round: 3,
  base: 'c0ffee02',
  commit: 'c0ffee03',
  files: [{path: 'src/sampler.rs', change: 'modified'}],
};
const PATCH = [
  'diff --git a/src/sampler.rs b/src/sampler.rs',
  'index 1a2b3c4..5d6e7f8 100644',
  '--- a/src/sampler.rs',
  '+++ b/src/sampler.rs',
  '@@ -60,3 +60,3 @@ impl Sampler {',
  '     let x = 1;',
  '-    device_sync();',
  '+    device_fence(self.stream);',
  '     x',
  '',
].join('\n');
const ROW: RoundRow = {
  round: 3,
  state: 'reverted',
  title: 'Skip the post-sampling device sync',
  hypothesis: null,
  value: null,
  delta: null,
  before: {value: 1150, round: 2},
};
const KEY = patchKey('c0ffee02', 'c0ffee03', 'src/sampler.rs');
const filesOf = (design: DesignRound, slot?: PatchSlot) => {
  const model = changesModel(ROW, design, slot === undefined ? {} : {[KEY]: slot});
  assert.ok(model.kind === 'files');
  return model;
};

test('a unified patch as numbered lines; headers before the first hunk are dropped', () => {
  assert.deepEqual(parsePatch(PATCH), [
    {tone: 'hunk', text: '@@ -60,3 +60,3 @@ impl Sampler {', line: null},
    {tone: 'ctx', text: '    let x = 1;', line: 60},
    {tone: 'del', text: '    device_sync();', line: 61},
    {tone: 'add', text: '    device_fence(self.stream);', line: 61},
    {tone: 'ctx', text: '    x', line: 62},
  ]);
});

test('each file states its patch: loading, loaded, truncated, unavailable, failed', () => {
  const loading = filesOf(DESIGN);
  assert.equal(loading.against, 'the round 2 checkpoint');
  assert.equal(loading.commit, 'c0ffee03');
  assert.deepEqual(loading.files[0]?.body, {kind: 'loading'});
  assert.equal(loading.files[0]?.command, 'git diff c0ffee02 c0ffee03 -- src/sampler.rs');
  const patch = {base: 'c0ffee02', head: 'c0ffee03', path: 'src/sampler.rs'};
  const truncated = filesOf(DESIGN, {
    kind: 'loaded',
    patch: {...patch, patch: PATCH, truncated: true},
  });
  assert.deepEqual([truncated.files[0]?.added, truncated.files[0]?.removed], [1, 1]);
  assert.equal(
    truncated.files[0]?.body.kind === 'patch' && truncated.files[0].body.truncated,
    true,
  );
  assert.deepEqual(
    filesOf(DESIGN, {kind: 'loaded', patch: {...patch, patch: null}}).files[0]?.body,
    {
      kind: 'unavailable',
    },
  );
  assert.deepEqual(filesOf(DESIGN, {kind: 'loaded', patch: null}).files[0]?.body, {
    kind: 'unavailable',
  });
  assert.deepEqual(filesOf(DESIGN, {kind: 'error', message: 'timeout'}).files[0]?.body, {
    kind: 'error',
    message: 'timeout',
  });
});

test('no recorded range, a running round, no round, a rename, a range without a base', () => {
  assert.deepEqual(changesModel(ROW, undefined, {}), {kind: 'unresolved', round: 3});
  assert.deepEqual(changesModel(ROW, {round: 3, commit: 'c0ffee03', files: null}, {}), {
    kind: 'unresolved',
    round: 3,
  });
  assert.deepEqual(changesModel({...ROW, state: 'running'}, DESIGN, {}), {
    kind: 'running',
    round: 3,
  });
  assert.deepEqual(changesModel(undefined, DESIGN, {}), {kind: 'none'});
  const renamed = filesOf({
    ...DESIGN,
    files: [{path: 'src/new.rs', change: 'renamed', renamed_from: 'src/old.rs'}],
  });
  assert.equal(renamed.files[0]?.command, 'git diff c0ffee02 c0ffee03 -- src/old.rs src/new.rs');
  assert.deepEqual(patchRequests(DESIGN), [
    {key: KEY, base: 'c0ffee02', head: 'c0ffee03', path: 'src/sampler.rs'},
  ]);
  const baseless: DesignRound = {round: 3, commit: 'c0ffee03', files: DESIGN.files ?? null};
  assert.deepEqual(patchRequests(baseless), []);
  const model = filesOf(baseless);
  assert.deepEqual(model.files[0]?.body, {kind: 'unavailable'});
  assert.equal(model.files[0]?.command, 'git show c0ffee03 -- src/sampler.rs');
});

test('a rejected patch request settles as an error slot, not an unhandled rejection', async () => {
  const request = {key: KEY, base: 'c0ffee02', head: 'c0ffee03', path: 'src/sampler.rs'};
  const slot = await loadPatchSlot(async () => {
    throw new Error('gateway closed');
  }, request);
  assert.deepEqual(slot, {kind: 'error', message: 'gateway closed'});
});

test('reproduction commands quote paths a shell would split, rename sources included', () => {
  assert.equal(shellQuote('src/lib.rs'), 'src/lib.rs');
  assert.equal(shellQuote('docs/my notes.md'), "'docs/my notes.md'");
  assert.equal(shellQuote("it's.rs"), "'it'\\''s.rs'");
  const renamed = filesOf({
    ...DESIGN,
    files: [{path: 'src/new name.rs', change: 'renamed', renamed_from: "src/old's.rs"}],
  });
  assert.equal(
    renamed.files[0]?.command,
    "git diff c0ffee02 c0ffee03 -- 'src/old'\\''s.rs' 'src/new name.rs'",
  );
});
