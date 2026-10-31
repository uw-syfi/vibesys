import assert from 'node:assert/strict';
import {test} from 'node:test';
import * as proto from '@vibesys/proto';

// Live state belongs on Subscribe, as a topic. A stream added here is a new set of lifetime rules
// (lease, retries, ghosts), so it must be argued for in this list, with the gateway's own
// (gateway/internal/server/subscribe_test.go).
const STREAMS = {
  subscribe: 'the live-state primitive itself',
  tail: 'every record matters, so values cannot conflate',
  tailStream: 'a byte stream, where a skipped byte corrupts the output',
  watch: "a Python operation's progress stream, ending with the operation",
};

test('the host service streams exactly where a value cannot be skipped', () => {
  const streaming = proto.HostService.methods
    .filter(method => method.methodKind !== 'unary')
    .map(method => method.localName)
    .sort();
  assert.deepEqual(streaming, Object.keys(STREAMS).sort());
});

test('the run list has no stream of its own: it is the runs topic', () => {
  assert.equal(proto.HostService.method.watchRuns, undefined);
  assert.ok('runs' in proto.topicPolicies);
});
