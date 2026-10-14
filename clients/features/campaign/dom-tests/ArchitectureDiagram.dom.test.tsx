/** Public architecture-detail interaction in a DOM: modal ownership and focus restoration. */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {type ArchitectureGraphDocument, parseArchitectureGraph} from '@vibesys/contracts';
import {Window} from 'happy-dom';
import {act, useState} from 'react';
import type {Root} from 'react-dom/client';

const graph = parseArchitectureGraph(
  JSON.parse(
    readFileSync(resolve(import.meta.dir, '../src/fixtures/architecture-queue.json'), 'utf8'),
  ),
);
const testWindow = new Window();
const installed = Object.getOwnPropertyNames(testWindow).filter(name => !(name in globalThis));
const eventDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'Event');
const customEventDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'CustomEvent');
let ArchitectureDiagram: typeof import('../src/renderer/index.js').ArchitectureDiagram;
let createRoot: typeof import('react-dom/client').createRoot;

beforeAll(async () => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(testWindow, name),
    });
  }
  Object.assign(globalThis, {
    window: testWindow,
    Event: testWindow.Event,
    CustomEvent: testWindow.CustomEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  ({createRoot} = await import('react-dom/client'));
  ({ArchitectureDiagram} = await import('../src/renderer/index.js'));
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  restoreGlobal('Event', eventDescriptor);
  restoreGlobal('CustomEvent', customEventDescriptor);
  await testWindow.happyDOM.close();
});

test('each diagram owns its IDs and returns focus to its own graph trigger', async () => {
  const mounted = await mountDiagram(graph);
  const triggers = mounted.container.querySelectorAll<HTMLButtonElement>(
    '[data-component-id="queue.producer"] .architecture-node-trigger',
  );
  expect(triggers).toHaveLength(2);
  const firstTrigger = triggers.item(0);
  const secondTrigger = triggers.item(1);
  expect(firstTrigger.id).not.toBe(secondTrigger.id);
  expect(firstTrigger.getAttribute('aria-controls')).not.toBe(
    secondTrigger.getAttribute('aria-controls'),
  );
  const ids = [...document.querySelectorAll<HTMLElement>('[id]')].map(element => element.id);
  expect(new Set(ids).size).toBe(ids.length);
  secondTrigger.focus();

  await act(async () => secondTrigger.click());
  const close = document.querySelector<HTMLButtonElement>(
    'button[aria-label="Close component details"]',
  );
  if (close === null) throw new Error('component detail close control missing');
  const detailsId = secondTrigger.getAttribute('aria-controls');
  if (detailsId === null) throw new Error('component detail relationship missing');
  expect(document.getElementById(detailsId)).not.toBeNull();
  expect(firstTrigger.getAttribute('aria-expanded')).toBe('false');
  expect(secondTrigger.getAttribute('aria-expanded')).toBe('true');
  close.focus();
  expect(document.activeElement).toBe(close);

  await act(async () => close.click());

  expect(document.querySelector('[aria-label="Close component details"]')).toBeNull();
  expect(secondTrigger.getAttribute('aria-expanded')).toBe('false');
  expect(document.activeElement).toBe(secondTrigger);
  expect(document.activeElement).not.toBe(firstTrigger);
  await unmount(mounted);
});

function ControlledDiagram({document}: {readonly document: ArchitectureGraphDocument}) {
  const [selectedComponentId, setSelectedComponentId] = useState<string | null>(null);
  return (
    <ArchitectureDiagram
      graph={document}
      cursorMeasurementId="queue-copy-elision"
      cursorSequence={2}
      selectedComponentId={selectedComponentId}
      onSelectedComponentId={setSelectedComponentId}
    />
  );
}

function TwoControlledDiagrams({document}: {readonly document: ArchitectureGraphDocument}) {
  return (
    <>
      <ControlledDiagram document={document} />
      <ControlledDiagram document={document} />
    </>
  );
}

interface MountedDiagram {
  readonly container: HTMLElement;
  readonly root: Root;
}

async function mountDiagram(document: ArchitectureGraphDocument): Promise<MountedDiagram> {
  const container = globalThis.document.createElement('div');
  globalThis.document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<TwoControlledDiagrams document={document} />));
  return {container, root};
}

async function unmount({container, root}: MountedDiagram): Promise<void> {
  await act(async () => root.unmount());
  container.remove();
}

function restoreGlobal(name: 'Event' | 'CustomEvent', descriptor: PropertyDescriptor | undefined) {
  if (descriptor === undefined) {
    Reflect.deleteProperty(globalThis, name);
  } else {
    Object.defineProperty(globalThis, name, descriptor);
  }
}
