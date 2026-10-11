import {describe, expect, test} from 'bun:test';
import {checkModel, Seeded} from '@vibesys/core-state/testing';
import type {HostKey} from '../../host-settings.js';
import {randomChrome} from '../../testing/fake-chrome.js';
import type {ChromeState, WelcomeHost, WelcomeResult} from '../../welcome-protocol.js';
import {
  type HostAction,
  type HostRequest,
  type HostState,
  hostAwaited,
  hostInitial,
  hostInvariants,
  hostStep,
} from './host.js';

const HOSTS: readonly HostKey[] = ['local', 'ssh:gpu-box', 'ssh:login-node'];

function hostData(key: HostKey, checkout: string | null): WelcomeHost {
  return {
    key,
    label: key,
    checkout,
    suggestedCheckout: checkout === null ? '/home/me/vibesys' : null,
    runs: [],
    projects: [],
  };
}

const WELCOME: ChromeState = {mode: 'welcome', attached: null, stops: {}};

function fails<T>(random: Seeded): WelcomeResult<T> {
  return {ok: false, error: 'no', authNeeded: random.bool()};
}

function randomHostAction(random: Seeded, issued: readonly number[], state: HostState): HostAction {
  const token = random.pick(issued);
  return random.pick<HostAction>([
    {type: 'show', host: random.pick(HOSTS)},
    {type: 'reload'},
    {type: 'tick'},
    {type: 'chrome', chrome: random.bool() ? WELCOME : randomChrome(random)},
    {type: 'edit-checkout'},
    {type: 'checkout-input', value: random.pick(['', '/a', '/b'])},
    {type: 'cancel-checkout'},
    {type: 'submit-checkout'},
    {type: 'sign-in'},
    {
      type: 'host-reply',
      token,
      // Only the awaited reply may apply, and that one is for the host shown.
      result: random.bool()
        ? {ok: true, value: hostData(state.host, random.bool() ? '/src/v' : null)}
        : fails(random),
    },
    {
      type: 'checkout-reply',
      token,
      result: random.bool() ? {ok: true, value: '/a'} : fails(random),
    },
    {type: 'sign-in-reply', token, result: random.bool() ? {ok: true, value: null} : fails(random)},
  ]);
}

function fold(state: HostState, actions: readonly HostAction[]) {
  const requests: HostRequest[] = [];
  for (const action of actions) {
    const next = hostStep(state, action);
    state = next.state;
    requests.push(...next.requests);
  }
  return {state, requests};
}

describe('host panel', () => {
  test('regression: a slow reply for the host shown before never draws over the host shown now', () => {
    const shown = fold(hostInitial('local'), [
      {type: 'show', host: 'local'},
      {type: 'show', host: 'ssh:gpu-box'},
    ]);
    const [local, gpu] = shown.requests;
    if (local === undefined || gpu === undefined) throw new Error('two loads expected');
    const late = hostStep(shown.state, {
      type: 'host-reply',
      token: local.token,
      result: {ok: true, value: hostData('local', '/src/vibesys')},
    });
    expect(late.state).toBe(shown.state);
    const ready = hostStep(shown.state, {
      type: 'host-reply',
      token: gpu.token,
      result: {ok: true, value: hostData('ssh:gpu-box', '/srv/vibesys')},
    }).state;
    expect(ready.panel.kind === 'ready' && ready.panel.data.key).toBe('ssh:gpu-box');
  });

  test('regression: coming back from a run reloads the attached run host panel', () => {
    const random = new Seeded(17);
    for (let round = 0; round < 300; round += 1) {
      const shown = random.pick(HOSTS);
      const before = randomChrome(random);
      const after = randomChrome(random);
      const state = fold(hostInitial(shown), [
        {type: 'show', host: shown},
        {type: 'chrome', chrome: before},
      ]).state;
      const next = hostStep(state, {type: 'chrome', chrome: after});
      const returned = before.mode === 'run' && after.mode === 'welcome';
      expect(next.requests.map(request => request.host)).toEqual(
        returned ? [after.attached?.host ?? shown] : [],
      );
    }
  });

  test('regression: changing a checkout can be cancelled; the first one cannot', () => {
    const loaded = fold(hostInitial('local'), [{type: 'show', host: 'local'}]);
    const token = loaded.requests[0]?.token ?? -1;
    const ready = hostStep(loaded.state, {
      type: 'host-reply',
      token,
      result: {ok: true, value: hostData('local', '/src/vibesys')},
    }).state;
    const editing = hostStep(ready, {type: 'edit-checkout'}).state;
    expect(editing.panel.kind === 'ready' && editing.panel.checkout?.value).toBe('/src/vibesys');
    expect(hostStep(editing, {type: 'cancel-checkout'}).state).toEqual(ready);
    const first = fold(hostInitial('ssh:gpu-box'), [{type: 'show', host: 'ssh:gpu-box'}]);
    const asks = hostStep(first.state, {
      type: 'host-reply',
      token: first.requests[0]?.token ?? -1,
      result: {ok: true, value: hostData('ssh:gpu-box', null)},
    }).state;
    expect(hostStep(asks, {type: 'cancel-checkout'}).state).toBe(asks);
  });

  test('model: invariants hold and stale replies are dropped over random sequences', () => {
    checkModel({
      initial: hostInitial('local'),
      step: hostStep,
      action: randomHostAction,
      issues: request => request.token,
      awaited: hostAwaited,
      reply: action => {
        if (action.type === 'host-reply') return {slot: 'host', token: action.token};
        if (action.type === 'checkout-reply') return {slot: 'checkout', token: action.token};
        if (action.type === 'sign-in-reply') return {slot: 'signIn', token: action.token};
        return null;
      },
      invariants: hostInvariants,
      also: (_before, _action, after, requests) => {
        for (const request of requests) expect(request.host).toBe(after.host);
      },
    });
  });
});
