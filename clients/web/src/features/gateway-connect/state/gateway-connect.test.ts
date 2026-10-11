import {describe, expect, test} from 'bun:test';
import {checkModel} from '../../../shared/testing/model.js';
import {
  type GatewayConnectAction,
  type GatewayConnectRequest,
  gatewayConnectInitial,
  gatewayConnectInvariants,
  gatewayConnectStep,
} from './gateway-connect.js';

const PAGE = 'http://127.0.0.1:5173/';
const URLS = [
  '',
  'http://127.0.0.1:8765/?token=secret',
  'http://127.0.0.1:8765/',
  'file:///tmp/run',
  'not a url',
];

describe('gatewayConnectStep', () => {
  test('exchanges one capability at a time and drops stale replies', () => {
    checkModel({
      initial: gatewayConnectInitial(PAGE, null),
      step: gatewayConnectStep,
      action: (random, issued): GatewayConnectAction => {
        switch (random.int(5)) {
          case 0:
            return {type: 'edit', value: random.pick(URLS)};
          case 1:
            return {type: 'submit'};
          case 2:
            return {type: 'cancel'};
          case 3:
            return {type: 'retry'};
          default:
            return {
              type: 'bootstrap-reply',
              token: random.pick(issued),
              result: random.bool()
                ? {ok: true, browserSession: 'browser-session'}
                : {ok: false, error: 'Gateway rejected the capability (HTTP 403)'},
            };
        }
      },
      issues: request => (request.kind === 'bootstrap' ? request.token : null),
      reply: (state, action) =>
        action.type === 'bootstrap-reply'
          ? {token: action.token, awaited: state.kind === 'connecting' ? state.token : null}
          : null,
      invariants: gatewayConnectInvariants,
      also: (before, action, after, requests: readonly GatewayConnectRequest[]) => {
        // A submit while an exchange is pending, or after the hand-off, asks for nothing.
        if (action.type === 'submit' && before.kind !== 'editing') expect(requests).toEqual([]);
        for (const request of requests) {
          switch (request.kind) {
            case 'bootstrap':
              // Only a capability-bearing URL is ever exchanged, never a WebSocket URL.
              expect(new URL(request.url).searchParams.get('token')).not.toBeNull();
              expect(after.kind).toBe('connecting');
              break;
            case 'cancel-bootstrap':
              expect(before.kind).toBe('connecting');
              expect(after.kind).toBe('editing');
              break;
            case 'connected':
              expect(request.connection.webSocketUrl).not.toContain('token');
              expect(after.kind).toBe('connected');
              break;
          }
        }
      },
    });
  });

  test('names what is wrong with a pasted URL and keeps it editable', () => {
    const step = gatewayConnectStep(
      {...gatewayConnectInitial(PAGE, null), url: 'http://127.0.0.1:8765/'},
      {type: 'submit'},
    );
    expect(step.requests).toEqual([]);
    expect(step.state).toMatchObject({
      kind: 'editing',
      error: 'The gateway URL must include its capability token',
    });
  });

  test('hands a clean connection to the page owner on a successful exchange', () => {
    const typed = gatewayConnectStep(gatewayConnectInitial(PAGE, 'earlier failure'), {
      type: 'edit',
      value: 'http://127.0.0.1:8765/?token=secret',
    }).state;
    const submitted = gatewayConnectStep(typed, {type: 'submit'});
    expect(submitted.requests).toEqual([
      {kind: 'bootstrap', token: 1, url: 'http://127.0.0.1:8765/?token=secret'},
    ]);
    const done = gatewayConnectStep(submitted.state, {
      type: 'bootstrap-reply',
      token: 1,
      result: {ok: true, browserSession: 'browser-session'},
    });
    expect(done.requests).toEqual([
      {
        kind: 'connected',
        connection: {
          gatewayUrl: 'http://127.0.0.1:8765/',
          cleanPageUrl: 'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F',
          webSocketUrl: 'ws://127.0.0.1:8765/ws',
          browserSession: 'browser-session',
        },
      },
    ]);
  });

  test('cancels one attempt, drops its late reply, and retries the retained target', () => {
    const typed = gatewayConnectStep(gatewayConnectInitial(PAGE, null), {
      type: 'edit',
      value: 'http://127.0.0.1:8765/?token=secret',
    }).state;
    const submitted = gatewayConnectStep(typed, {type: 'submit'});
    const cancelled = gatewayConnectStep(submitted.state, {type: 'cancel'});

    expect(cancelled.requests).toEqual([{kind: 'cancel-bootstrap', token: 1}]);
    expect(cancelled.state).toMatchObject({
      kind: 'editing',
      error: 'Gateway connection cancelled. Retry when ready.',
      retryTarget: {bootstrapUrl: 'http://127.0.0.1:8765/?token=secret'},
    });
    const late = gatewayConnectStep(cancelled.state, {
      type: 'bootstrap-reply',
      token: 1,
      result: {ok: true, browserSession: 'late-session'},
    });
    expect(late).toEqual({state: cancelled.state, requests: []});

    const retried = gatewayConnectStep(cancelled.state, {type: 'retry'});
    expect(retried.state).toMatchObject({kind: 'connecting', token: 2});
    expect(retried.requests).toEqual([
      {kind: 'bootstrap', token: 2, url: 'http://127.0.0.1:8765/?token=secret'},
    ]);
  });
});
