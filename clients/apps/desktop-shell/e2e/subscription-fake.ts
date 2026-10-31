/** The page-side subscription port for a world: bundled with the real codecs, as the gateway's is. */
import {fileURLToPath} from 'node:url';
import type {Page} from '@playwright/test';
import {buildSync} from 'esbuild';
import type {HostFeed, SubscriptionSeed} from './subscription-preload.js';

export type {SubscriptionSeed};

let preload: string | undefined;

/** An init script that installs `window.vibesysSubscription` serving `seed`. */
export function subscriptionScript(seed: SubscriptionSeed): string {
  preload ??= buildSync({
    entryPoints: [fileURLToPath(new URL('./subscription-preload.ts', import.meta.url))],
    bundle: true,
    write: false,
    format: 'iife',
    globalName: 'fixtureSubscriptions',
    platform: 'browser',
    conditions: ['@vibesys/source'],
    target: 'es2022',
  }).outputFiles[0]?.text;
  if (preload === undefined) throw new Error('subscription fixture bundle is missing');
  return `${preload}\nfixtureSubscriptions.installSubscriptions(${JSON.stringify(seed)});`;
}

interface FakeSubscriptions {
  set(host: string, feed: HostFeed): void;
  retried(): string[];
  observing(host: string): number;
}

/** The scripted hosts, as a test drives them. */
export const hosts = {
  set: (page: Page, host: string, feed: HostFeed) =>
    page.evaluate(
      ([name, next]) =>
        (window as unknown as {fakeSubscriptions: FakeSubscriptions}).fakeSubscriptions.set(
          name as string,
          next as HostFeed,
        ),
      [host, feed] as const,
    ),
  retried: (page: Page) =>
    page.evaluate(() =>
      (window as unknown as {fakeSubscriptions: FakeSubscriptions}).fakeSubscriptions.retried(),
    ),
  observing: (page: Page, host: string) =>
    page.evaluate(
      name =>
        (window as unknown as {fakeSubscriptions: FakeSubscriptions}).fakeSubscriptions.observing(
          name,
        ),
      host,
    ),
};
