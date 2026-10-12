import {describe, expect, test} from 'bun:test';
import type {PageVisibility} from '@vibesys/run-session';
import {disconnect, sessionWith} from '@vibesys/run-session/testing';
import {
  type BrowserDocument,
  type BrowserNavigator,
  type BrowserWindow,
  createBrowserLifecycle,
} from './lifecycle.js';

class FakeDocument implements BrowserDocument {
  #visibilityState: PageVisibility = 'visible';
  readonly #listeners = new Set<() => void>();

  get visibilityState(): PageVisibility {
    return this.#visibilityState;
  }

  addEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.#listeners.add(listener);
  }

  removeEventListener(_type: 'visibilitychange', listener: () => void): void {
    this.#listeners.delete(listener);
  }

  setVisibility(state: PageVisibility): void {
    this.#visibilityState = state;
    for (const listener of this.#listeners) listener();
  }

  get listenerCount(): number {
    return this.#listeners.size;
  }
}

class FakeWindow implements BrowserWindow {
  readonly #listeners: Record<'online' | 'offline', Set<() => void>> = {
    online: new Set(),
    offline: new Set(),
  };

  addEventListener(type: 'online' | 'offline', listener: () => void): void {
    this.#listeners[type].add(listener);
  }

  removeEventListener(type: 'online' | 'offline', listener: () => void): void {
    this.#listeners[type].delete(listener);
  }

  emit(type: 'online' | 'offline'): void {
    for (const listener of this.#listeners[type]) listener();
  }

  listenerCount(type: 'online' | 'offline'): number {
    return this.#listeners[type].size;
  }
}

class FakeNavigator implements BrowserNavigator {
  #online = true;

  get onLine(): boolean {
    return this.#online;
  }

  setOnline(online: boolean): void {
    this.#online = online;
  }
}

describe('browserLifecycle', () => {
  test('wakes a hidden stale session from the document and releases both targets on close', async () => {
    const page = new FakeDocument();
    const events = new FakeWindow();
    const network = new FakeNavigator();
    const {session, transport} = sessionWith(createBrowserLifecycle(page, events, network), {
      reconnectDelaysMs: [],
    });

    await session.start();
    page.setVisibility('hidden');
    network.setOnline(false);
    events.emit('offline');
    transport.dropControlChannel(disconnect('backgrounded'));
    transport.subscriptions[0]?.onDisconnect(disconnect('backgrounded'));
    expect(session.getState().status).toBe('stale');

    network.setOnline(true);
    events.emit('online');
    expect(transport.reconnectCalls).toBe(0);

    page.setVisibility('visible');
    expect(transport.reconnectCalls).toBe(1);
    expect(page.listenerCount).toBe(1);
    expect(events.listenerCount('online')).toBe(1);
    expect(events.listenerCount('offline')).toBe(1);

    await session.close();
    expect(page.listenerCount).toBe(0);
    expect(events.listenerCount('online')).toBe(0);
    expect(events.listenerCount('offline')).toBe(0);
  });
});
