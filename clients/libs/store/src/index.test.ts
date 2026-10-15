import {describe, expect, test} from 'bun:test';
import {createListenerDelivery, createStore} from './index.js';

describe('createListenerDelivery', () => {
  test('delivers to every eligible subscriber before reporting each possible failure position', () => {
    for (let failing = 0; failing < 5; failing += 1) {
      const delivery = createListenerDelivery();
      const failure = new Error(`subscriber ${failing} failed`);
      const heard: number[] = [];
      for (let index = 0; index < 5; index += 1) {
        delivery.subscribe(() => {
          heard.push(index);
          if (index === failing) throw failure;
        });
      }

      let reported: unknown;
      try {
        delivery.notify();
      } catch (cause) {
        reported = cause;
      }

      expect(reported).toBe(failure);
      expect(heard).toEqual([0, 1, 2, 3, 4]);
    }
  });

  test('reports multiple subscriber failures together in delivery order', () => {
    const delivery = createListenerDelivery();
    const first = new Error('first');
    const second = new Error('second');
    delivery.subscribe(() => {
      throw first;
    });
    delivery.subscribe(() => undefined);
    delivery.subscribe(() => {
      throw second;
    });

    let reported: unknown;
    try {
      delivery.notify();
    } catch (cause) {
      reported = cause;
    }

    expect(reported).toBeInstanceOf(AggregateError);
    expect((reported as AggregateError).errors).toEqual([first, second]);
  });

  test('finishes the starting subscription snapshot before applying fanout mutations', () => {
    const delivery = createListenerDelivery();
    const trace: string[] = [];
    let changed = false;
    const added = () => trace.push('added');
    const removed = () => trace.push('removed');
    let unsubscribeRemoved: () => void = () => undefined;
    delivery.subscribe(() => {
      trace.push('first');
      if (changed) return;
      changed = true;
      unsubscribeRemoved();
      delivery.subscribe(added);
      delivery.subscribe(removed);
    });
    unsubscribeRemoved = delivery.subscribe(removed);

    delivery.notify();
    delivery.notify();

    expect(trace).toEqual(['first', 'removed', 'first', 'added', 'removed']);
  });

  test('gives a reentrant notification its own snapshot', () => {
    const delivery = createListenerDelivery();
    const trace: string[] = [];
    let nested = false;
    delivery.subscribe(() => {
      trace.push('first');
      if (nested) return;
      nested = true;
      delivery.notify();
    });
    delivery.subscribe(() => trace.push('second'));

    delivery.notify();

    expect(trace).toEqual(['first', 'first', 'second', 'second']);
  });

  test('owns duplicate callback subscriptions and unsubscriptions independently', () => {
    const delivery = createListenerDelivery();
    let heard = 0;
    const listener = () => {
      heard += 1;
    };
    const unsubscribeFirst = delivery.subscribe(listener);
    const unsubscribeSecond = delivery.subscribe(listener);

    unsubscribeFirst();
    delivery.notify();
    expect(heard).toBe(1);

    unsubscribeSecond();
    delivery.notify();
    expect(heard).toBe(1);
  });
});

describe('createStore', () => {
  test('performs committed requests and notifies later subscribers before reporting a subscriber failure', () => {
    const failure = new Error('subscriber failed');
    const trace: string[] = [];
    const store = createStore(
      (_state: string, action: string) => ({
        state: action,
        requests: ['load-first', 'load-second'],
      }),
      'idle',
      request => trace.push(`perform:${request}`),
    );
    store.subscribe(() => {
      trace.push(`first:${store.getState()}`);
      throw failure;
    });
    store.subscribe(() => trace.push(`second:${store.getState()}`));

    let reported: unknown;
    try {
      store.dispatch('loading');
    } catch (cause) {
      reported = cause;
    }

    expect(reported).toBe(failure);
    expect(store.getState()).toBe('loading');
    expect(trace).toEqual([
      'first:loading',
      'second:loading',
      'perform:load-first',
      'perform:load-second',
    ]);
  });

  test('finishes reentrant dispatches and their requests before resuming the outer delivery', () => {
    const failure = new Error('ready subscriber failed');
    const trace: string[] = [];
    const store = createStore(
      (_state: string, action: string) => ({state: action, requests: [action]}),
      'idle',
      request => trace.push(`perform:${request}`),
    );
    store.subscribe(() => {
      const state = store.getState();
      trace.push(`first:${state}`);
      if (state === 'loading') store.dispatch('ready');
      if (state === 'ready') throw failure;
    });
    store.subscribe(() => trace.push(`second:${store.getState()}`));

    let reported: unknown;
    try {
      store.dispatch('loading');
    } catch (cause) {
      reported = cause;
    }

    expect(reported).toBe(failure);
    expect(store.getState()).toBe('ready');
    expect(trace).toEqual([
      'first:loading',
      'first:ready',
      'second:ready',
      'perform:ready',
      'second:ready',
      'perform:loading',
    ]);
  });

  test('does not let a synchronous reply subscriber failure strand later outer requests', () => {
    const failure = new Error('reply subscriber failed');
    const trace: string[] = [];
    const store = createStore(
      (_state: string, action: string) => ({
        state: action,
        requests:
          action === 'loading' ? (['reply', 'outer-tail'] as const) : (['inner-request'] as const),
      }),
      'idle',
      (request, dispatch) => {
        trace.push(`perform:${request}`);
        if (request === 'reply') dispatch('ready');
      },
    );
    store.subscribe(() => {
      const state = store.getState();
      trace.push(`first:${state}`);
      if (state === 'ready') throw failure;
    });
    store.subscribe(() => trace.push(`second:${store.getState()}`));

    let reported: unknown;
    try {
      store.dispatch('loading');
    } catch (cause) {
      reported = cause;
    }

    expect(reported).toBe(failure);
    expect(store.getState()).toBe('ready');
    expect(trace).toEqual([
      'first:loading',
      'second:loading',
      'perform:reply',
      'first:ready',
      'second:ready',
      'perform:inner-request',
      'perform:outer-tail',
    ]);
  });

  test('reports subscriber and request failures without masking either cause', () => {
    const subscriberFailure = new Error('subscriber failed');
    const requestFailure = new Error('request failed');
    const store = createStore(
      (_state: string, action: string) => ({state: action, requests: ['load']}),
      'idle',
      () => {
        throw requestFailure;
      },
    );
    store.subscribe(() => {
      throw subscriberFailure;
    });

    let reported: unknown;
    try {
      store.dispatch('loading');
    } catch (cause) {
      reported = cause;
    }

    expect(reported).toBeInstanceOf(AggregateError);
    expect((reported as AggregateError).errors).toEqual([subscriberFailure, requestFailure]);
  });
});
