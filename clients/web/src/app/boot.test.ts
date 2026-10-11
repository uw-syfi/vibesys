import {describe, expect, test} from 'bun:test';
import {serializeBrowserSession} from '../entities/gateway.js';
import {bootPlan} from './boot.js';

const NOTHING = {gateway: null, browserSession: null};

describe('bootPlan', () => {
  test('starts the replay demo when nothing names a gateway', () => {
    expect(bootPlan('http://127.0.0.1:5173/', NOTHING)).toEqual({kind: 'demo', error: null});
  });

  test('starts the demo with the reason when the named gateway is refused', () => {
    expect(
      bootPlan('https://viewer.test/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F', NOTHING),
    ).toEqual({kind: 'demo', error: 'An HTTPS browser page requires an HTTPS gateway URL'});
  });

  test('exchanges a nested launch capability before going live', () => {
    const plan = bootPlan(
      'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret',
      NOTHING,
    );
    expect(plan.kind).toBe('bootstrap');
    if (plan.kind === 'bootstrap') {
      expect(plan.target.bootstrapUrl).toBe('http://127.0.0.1:8765/?token=secret');
    }
  });

  test('goes live on a remembered gateway with its own remembered credential only', () => {
    const gateway = 'http://127.0.0.1:8765/';
    const remembered = {
      gateway,
      browserSession: serializeBrowserSession(gateway, 'browser-session'),
    };
    expect(bootPlan('http://127.0.0.1:8765/', remembered)).toMatchObject({
      kind: 'live',
      browserSession: 'browser-session',
    });
    const foreign = {...remembered, browserSession: serializeBrowserSession('http://x.test/', 's')};
    expect(bootPlan('http://127.0.0.1:8765/', foreign)).toMatchObject({
      kind: 'live',
      browserSession: null,
    });
  });
});
