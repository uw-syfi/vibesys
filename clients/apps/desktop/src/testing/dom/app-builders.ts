/**
 * The one place a full `HostSnapshot` and `AppCommands` are written out for tests. A test names only
 * what it is about, as overrides; a member added to either type is given its default here, so no
 * test changes. The `no-full-app-literals` lint rule keeps other test files on these builders.
 */
import {create} from '@bufbuild/protobuf';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import {
  DORMANT_GENERIC_RUN_INDEX,
  emptyState,
  NOT_PROVISIONING,
  NOT_READ,
} from '../../state/index.js';
import type {AppCommands, HostSnapshot} from '../../ui/orchestrator/app.js';

/** A live, ready host with no data and no open streams, unless `overrides` say otherwise. */
export function hostSnapshotOf(overrides: Partial<HostSnapshot> = {}): HostSnapshot {
  return {
    key: 'local',
    label: 'This machine',
    model: {data: emptyState, agentCursors: {}},
    connection: {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
    freshness: {status: 'live', lastUpdateMs: null},
    runsFeed: {_tag: 'Live'},
    runIndex: DORMANT_GENERIC_RUN_INDEX,
    repositoryChecked: undefined,
    directories: new Map(),
    catalogue: NOT_READ,
    evalPlaces: NOT_READ,
    repositoriesDirectory: '',
    provision: NOT_PROVISIONING,
    agentFeeds: {},
    costFeeds: {},
    runHistory: {_tag: 'Idle'},
    history: {earlier: {_tag: 'Idle'}, wholes: {}},
    ...overrides,
  };
}

/** Commands that do nothing and have no progress, unless `overrides` say otherwise. */
export function appCommandsOf(overrides: Partial<AppCommands> = {}): AppCommands {
  return {
    start: undefined,
    stop: {},
    agentStop: {},
    send: {},
    update: {},
    complete: {},
    startRun: () => undefined,
    checkRepository: () => undefined,
    listDirectory: () => undefined,
    loadNewRun: () => undefined,
    startFromExample: () => undefined,
    setUpInNewRepository: () => undefined,
    clearStart: () => undefined,
    forgetProvision: () => undefined,
    retryStart: () => undefined,
    stopRun: () => undefined,
    retryStop: () => undefined,
    stopAgent: () => undefined,
    retryStopAgent: () => undefined,
    sendMessage: () => undefined,
    retrySend: () => undefined,
    updateRun: () => undefined,
    retryUpdate: () => undefined,
    beginSetup: () => undefined,
    completeSetup: () => undefined,
    retryComplete: () => undefined,
    loadEarlier: () => undefined,
    expand: () => undefined,
    ...overrides,
  };
}
