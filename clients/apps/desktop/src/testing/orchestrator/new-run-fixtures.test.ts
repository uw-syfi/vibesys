import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  AcceleratorModel,
  AcceleratorVendor,
  decodeValidated,
  EvalPlaceKind,
  EvalPlaceSchema,
  FitState,
  ListCatalogueResponseSchema,
  ListEvalPlacesResponseSchema,
  OffersSource,
  OffersUnreadableReason,
  ProvisionRefusal,
  ProvisionRepositoryRequestSchema,
  ProvisionRepositoryResponseSchema,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import {RpcStatus} from '../../core/errors.js';
import {daemonRecording, FakeDaemon, generateScenario, methodPathOf} from './index.js';
import {FIXTURE_REPOSITORIES_DIRECTORY, fitOf, fixturePlaces} from './new-run-fixtures.js';

const host = 'local' as never;

const answer = (
  recording: ReturnType<typeof daemonRecording>,
  method: string,
  request: Uint8Array,
) => {
  const steps = recording.respond({host, method: methodPathOf(method), request}, 0);
  const [step] = steps;
  if (steps.length !== 1 || step === undefined || !('frame' in step))
    throw new Error(`no frame from ${method}`);
  return step.frame;
};

const recording = () =>
  daemonRecording(new FakeDaemon(generateScenario({seed: 1, subagents: 1, live: false})));

describe('the fake host answers the New run calls', () => {
  it('serves places and a catalogue that meet the contract, each entry fitting as the places say', () => {
    const calls = recording();
    const places = decodeValidated(
      ListEvalPlacesResponseSchema,
      answer(calls, 'ListEvalPlaces', new Uint8Array()),
    );
    expect(places.places.map(each => each.name)).toEqual(['local', 'host-a', 'cluster-a']);
    const catalogue = decodeValidated(
      ListCatalogueResponseSchema,
      answer(calls, 'ListCatalogue', new Uint8Array()),
    );
    expect(catalogue.repositoriesDirectory).toBe(FIXTURE_REPOSITORIES_DIRECTORY);
    expect(catalogue.entries.length).toBeGreaterThan(0);
    for (const entry of catalogue.entries) {
      expect(entry.fit).toEqual(fitOf(entry.card ?? create(TaskCardSchema), places.places));
    }
    const states = new Set(catalogue.entries.map(entry => entry.fit?.state));
    expect(states).toContain(FitState.FITS);
    expect(states).toContain(FitState.NO_ACCELERATOR);
  });

  it('fits a vendor-only need to any model of the vendor, and counts the accelerators', () => {
    const places = fixturePlaces();
    const need = (model: AcceleratorModel, count: number) =>
      create(TaskCardSchema, {
        task: 't',
        hardware: {vendor: AcceleratorVendor.NVIDIA, model, count},
      });
    expect(fitOf(need(AcceleratorModel.UNSPECIFIED, 1), places).places).toEqual(['host-a']);
    expect(fitOf(need(AcceleratorModel.H100, 8), places).places).toEqual(['host-a']);
    expect(fitOf(need(AcceleratorModel.H100, 9), places).state).toBe(FitState.NO_ACCELERATOR);
    expect(fitOf(need(AcceleratorModel.A100, 1), places).state).toBe(FitState.NO_ACCELERATOR);
    expect(fitOf(need(AcceleratorModel.OTHER, 1), places).state).toBe(FitState.UNKNOWN_ACCELERATOR);
  });

  it('fits nothing on an unreadable place or an OTHER device, a CPU need included', () => {
    const unreadable = create(EvalPlaceSchema, {
      name: 'host-b',
      kind: EvalPlaceKind.SSH,
      state: {case: 'unreadable', value: {reason: OffersUnreadableReason.UNREACHABLE}},
    });
    const odd = create(EvalPlaceSchema, {
      name: 'cluster-b',
      kind: EvalPlaceKind.SLURM,
      offers: [{vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.OTHER, count: 8}],
      state: {case: 'known', value: {source: OffersSource.PROBED}},
    });
    const cpu = create(TaskCardSchema, {
      task: 'c',
      hardware: {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1},
    });
    const gpu = create(TaskCardSchema, {
      task: 'g',
      hardware: {vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.UNSPECIFIED, count: 1},
    });
    expect(fitOf(cpu, [unreadable]).state).toBe(FitState.PLACES_UNREADABLE);
    expect(fitOf(cpu, [unreadable, ...fixturePlaces()]).places).not.toContain('host-b');
    expect(fitOf(gpu, [odd]).state).toBe(FitState.NO_ACCELERATOR);
  });

  it('provisions a name once and refuses it the second time, and an example that needs a clone', () => {
    const calls = recording();
    const request = (name: string, example?: string) =>
      toBinary(
        ProvisionRepositoryRequestSchema,
        create(ProvisionRepositoryRequestSchema, {
          requestId: `r-${name}`,
          name,
          source:
            example === undefined ? {case: 'empty', value: {}} : {case: 'example', value: example},
        }),
      );
    const provision = (name: string, example?: string) =>
      decodeValidated(
        ProvisionRepositoryResponseSchema,
        answer(calls, 'ProvisionRepository', request(name, example)),
      );
    const first = provision('mine');
    expect(first.outcome).toMatchObject({
      case: 'provisioned',
      value: {repository: `${FIXTURE_REPOSITORIES_DIRECTORY}/mine`},
    });
    expect(provision('mine').outcome).toMatchObject({
      case: 'refused',
      value: {reason: ProvisionRefusal.NAME_TAKEN},
    });
    expect(provision('other', 'model-serving/ember').outcome).toMatchObject({
      case: 'refused',
      value: {reason: ProvisionRefusal.NEEDS_NETWORK},
    });
    expect(provision('third', 'model-serving/nothing').outcome).toMatchObject({
      case: 'refused',
      value: {reason: ProvisionRefusal.EXAMPLE_NOT_FOUND},
    });
  });

  it('still answers an unknown method as unimplemented', () => {
    const steps = recording().respond(
      {host, method: methodPathOf('Nope'), request: new Uint8Array()},
      0,
    );
    expect(steps).toEqual([
      {end: {message: `unknown method ${methodPathOf('Nope')}`, status: RpcStatus.UNIMPLEMENTED}},
    ]);
  });
});
