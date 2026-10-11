/**
 * The hosts feature's shell: its stores, each request performed through the injected
 * `HostsBridge` and its reply dispatched back with the request's token. Nothing here polls: a
 * host's runs arrive as pushes the app routes elsewhere.
 */

import {createStore, type Store} from '@vibesys/core-state';
import type {HostKey} from '@vibesys/entities';
import {settled} from '@vibesys/ipc';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  completionStep,
  type HostAction,
  type HostState,
  type HostsBridge,
  hostInitial,
  hostStep,
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerStep,
} from '../../state/index.js';

/** The host panel's store, showing `firstHost` until told otherwise. */
export function createHostStore(
  bridge: HostsBridge,
  firstHost: HostKey,
): Store<HostState, HostAction> {
  return createStore(hostStep, hostInitial(firstHost), (request, dispatch) => {
    switch (request.kind) {
      case 'host':
        void settled(bridge.host(request.host)).then(result =>
          dispatch({type: 'host-reply', token: request.token, result}),
        );
        return;
      case 'set-checkout':
        void settled(bridge.setCheckout(request.host, request.path, request.token)).then(result =>
          dispatch({type: 'checkout-reply', token: request.token, result}),
        );
        return;
      case 'cancel-checkout':
        // The cancelled change's own reply is stale; cancellation only revokes its main-process work.
        void settled(bridge.cancelCheckout(request.token));
        return;
      case 'sign-in':
        void settled(bridge.signIn(request.host)).then(result =>
          dispatch({type: 'sign-in-reply', token: request.token, result}),
        );
        return;
    }
  });
}

/** The "Connect to host" picker's store; choosing a host calls `showHost`. */
export function createPickerStore(
  bridge: HostsBridge,
  showHost: (host: HostKey) => void,
): Store<PickerState, PickerAction> {
  return createStore(pickerStep, PICKER_CLOSED, (request, dispatch) => {
    switch (request.kind) {
      case 'aliases':
        void settled(bridge.hosts()).then(result =>
          dispatch({type: 'aliases-reply', token: request.token, result}),
        );
        return;
      case 'show-host':
        showHost(request.host);
        return;
    }
  });
}

/** Completion listings, each asked after a pause; a newer request for a field replaces it. */
export function createCompletionStore(
  bridge: HostsBridge,
): Store<CompletionState, CompletionAction> {
  const pending = new Map<CompletionField, ReturnType<typeof setTimeout>>();
  return createStore(completionStep, COMPLETION_EMPTY, (request, dispatch) => {
    clearTimeout(pending.get(request.field));
    pending.set(
      request.field,
      setTimeout(() => {
        pending.delete(request.field);
        void settled(bridge.complete(request.host, request.directory)).then(result =>
          dispatch({type: 'reply', field: request.field, token: request.token, result}),
        );
      }, request.afterMs),
    );
  });
}
