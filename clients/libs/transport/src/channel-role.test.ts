import {describe, test} from 'node:test';
import {parseRunChannelRole, RUN_CHANNEL_ROLES} from './channel-role.js';
import {expect} from './test-support/expect.js';

describe('run channel roles', () => {
  test('round-trips every declared role', () => {
    for (const role of RUN_CHANNEL_ROLES) expect(parseRunChannelRole(role)).toBe(role);
  });

  test('rejects values outside the closed set', () => {
    for (const value of [undefined, null, '', 'control', 'host-control', 'inspect', 1, {}]) {
      expect(parseRunChannelRole(value)).toBeNull();
    }
  });
});
