import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ModelCatalogSchema, RunsSnapshotSchema} from '@vibesys/orchestrator-proto';
import {applySnapshot} from './fold.js';
import {emptyState} from './state.js';

const catalog = create(ModelCatalogSchema, {allowedModels: ['model-a']});

describe('the model catalog of a host', () => {
  it('is what its last snapshot said, and none for a snapshot that says nothing', () => {
    const said = applySnapshot(
      emptyState,
      create(RunsSnapshotSchema, {sequence: 5n, models: catalog}),
    );
    expect(said.models?.allowedModels).toEqual(['model-a']);
    const silent = applySnapshot(said, create(RunsSnapshotSchema, {sequence: 6n}));
    expect(silent.models).toBeNull();
  });

  it('is taken from a snapshot older than the state too, which still says what the host allows now', () => {
    const newer = applySnapshot(emptyState, create(RunsSnapshotSchema, {sequence: 9n}));
    const older = applySnapshot(newer, create(RunsSnapshotSchema, {sequence: 3n, models: catalog}));
    expect(older.models?.allowedModels).toEqual(['model-a']);
  });
});
