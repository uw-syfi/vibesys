/** Install a world's dependent Fakes in one browser initialization, in declaration order. */
import type {BrowserContext, Page} from '@playwright/test';
import {bridgeJitterFrom, installBridgeJitter} from './bridge-jitter.js';

export function initScript<T>(install: (seed: T) => void, seed: T): string {
  return `(${install.toString()})(${JSON.stringify(seed)});`;
}

export async function addInitScripts(
  target: Page | BrowserContext,
  sources: readonly string[],
): Promise<void> {
  // The amplifier goes last so that it wraps the methods the Fakes before it provide.
  const jitter = bridgeJitterFrom(process.env);
  const all = jitter === null ? sources : [...sources, initScript(installBridgeJitter, jitter)];
  await target.addInitScript({content: all.join('\n')});
}
