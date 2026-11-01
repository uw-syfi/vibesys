import {fileURLToPath} from 'node:url';
import type {Page} from '@playwright/test';
import {buildSync} from 'esbuild';
import type {WireFixture} from '../src/testing/gateway-fixture.js';

export {
  type GatewayBehavior,
  type WireFixture,
  wire,
} from '../src/testing/gateway-fixture.js';

let preload: string | undefined;

/** Bundle the real generated codecs into the page so the Fake cannot drift from the preload ABI. */
export function gatewayScript(input: WireFixture): string {
  preload ??= buildSync({
    entryPoints: [fileURLToPath(new URL('../src/testing/gateway-preload.ts', import.meta.url))],
    bundle: true,
    write: false,
    format: 'iife',
    globalName: 'fixtureGateway',
    platform: 'browser',
    conditions: ['@vibesys/source'],
    target: 'es2022',
  }).outputFiles[0]?.text;
  if (preload === undefined) throw new Error('gateway fixture bundle is missing');
  return `${preload}\nfixtureGateway.installGateway(${JSON.stringify(input)});`;
}
export async function installGateway(page: Page, input: WireFixture): Promise<void> {
  await page.addInitScript({content: gatewayScript(input)});
}
