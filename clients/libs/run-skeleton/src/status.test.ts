import {expect, test} from 'bun:test';
import {type LoadRefusal, refusalOf} from './index.js';

/** gRPC codes whose retry cannot help, by the loader's reading of the gateway's answers. */
const REFUSED = new Map<number, LoadRefusal>([
  [3, 'invalid'],
  [5, 'not-found'],
  [7, 'denied'],
  [8, 'too-large'],
  [9, 'invalid'],
  [11, 'invalid'],
  [12, 'unsupported'],
  [15, 'data-loss'],
  [16, 'denied'],
]);

test('every gRPC code is either a typed refusal or a failure worth retrying', () => {
  for (let code = 0; code <= 16; code += 1) {
    expect(refusalOf(Object.assign(new Error('refused'), {status: code}))).toBe(REFUSED.get(code));
  }
});

test('an error with no status, or a status that is not a code, is never a refusal', () => {
  for (const error of [new Error('link down'), {status: '5'}, {status: 99}, null, 'text'])
    expect(refusalOf(error)).toBeUndefined();
});
