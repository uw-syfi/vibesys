/** What the fake host answers the New run calls with, as encoded frames (see new-run-fixtures). */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ListCatalogueResponseSchema,
  ListEvalPlacesResponseSchema,
  ProvisionRepositoryRequestSchema,
  ProvisionRepositoryResponseSchema,
} from '@vibesys/orchestrator-proto';
import {RpcStatus} from '../../core/errors.js';
import type {Step} from '../transport/index.js';
import {
  fixtureCatalogueResponse,
  fixturePlacesResponse,
  provisionAnswer,
} from './new-run-fixtures.js';

/**
 * The repositories a fake host has made so far, so a name used twice is refused as taken. One set per
 * recording: a call that provisions is the one thing here that changes what a later call answers.
 */
export function newRunSteps(taken: Set<string>) {
  return (method: string, request: Uint8Array): ReadonlyArray<Step> => {
    switch (method) {
      case 'ListCatalogue':
        return [{frame: toBinary(ListCatalogueResponseSchema, fixtureCatalogueResponse())}];
      case 'ListEvalPlaces':
        return [{frame: toBinary(ListEvalPlacesResponseSchema, fixturePlacesResponse())}];
      case 'ProvisionRepository': {
        const answer = provisionAnswer(
          fromBinary(ProvisionRepositoryRequestSchema, request),
          taken,
        );
        if (answer.outcome.case === 'provisioned') taken.add(answer.outcome.value.repository);
        return [
          {
            frame: toBinary(
              ProvisionRepositoryResponseSchema,
              create(ProvisionRepositoryResponseSchema, answer),
            ),
          },
        ];
      }
      default:
        return [{end: {message: `unknown method ${method}`, status: RpcStatus.UNIMPLEMENTED}}];
    }
  };
}
