/** The words of the New run screen: labels, numbers and status words, never a sentence. */
import {
  AcceleratorModel,
  type EvalOffer,
  type EvalPlace,
  TaskDomain,
} from '@vibesys/orchestrator-proto';
import {
  type FitWord,
  type PlaceWord,
  type ProvisionProgress,
  readable,
} from '../../../state/index.js';

export const domainWord = (domain: TaskDomain): string => {
  switch (domain) {
    case TaskDomain.LLM_SERVING:
      return 'Model serving';
    case TaskDomain.DATA_STRUCTURES:
      return 'Data structures';
    case TaskDomain.DATABASES:
      return 'Databases';
    case TaskDomain.MICROSERVICES:
      return 'Microservices';
    default:
      return 'Other';
  }
};

const MODEL_WORDS: Readonly<Record<AcceleratorModel, string>> = {
  [AcceleratorModel.UNSPECIFIED]: '',
  [AcceleratorModel.MI210]: 'MI210',
  [AcceleratorModel.MI300A]: 'MI300A',
  [AcceleratorModel.MI300X]: 'MI300X',
  [AcceleratorModel.MI325X]: 'MI325X',
  [AcceleratorModel.H100]: 'H100',
  [AcceleratorModel.B200]: 'B200',
  [AcceleratorModel.L4]: 'L4',
  [AcceleratorModel.A100]: 'A100',
  [AcceleratorModel.TRAINIUM2]: 'Trainium2',
  [AcceleratorModel.APPLE_SILICON]: 'Apple Silicon',
  [AcceleratorModel.OTHER]: '',
};

/** One offer as `8× H100`. */
export const offerWords = (offer: EvalOffer): string => {
  const model = MODEL_WORDS[offer.model] || offer.deviceName || 'GPU';
  return `${offer.count}× ${model}`;
};

/** What an eval place gives, as a short figure: `8× H100`; `CPU` for a place with no accelerator. */
const hardwareWords = (place: EvalPlace): string =>
  place.offers.length === 0 ? 'CPU' : place.offers.map(offerWords).join(', ');

/** A place in the select: its name, its hardware, and the host's word for it on this entry. */
export const placeLabel = (place: EvalPlace, word: PlaceWord | null): string =>
  [place.name, readable(place) ? hardwareWords(place) : null, word]
    .filter((part): part is string => part !== null && part !== '')
    .join(' · ');

export const fitTone = (word: FitWord): 'ok' | 'warn' | 'err' =>
  word === 'Fits' ? 'ok' : word === 'Places unreadable' ? 'warn' : 'err';

const REFUSAL_WORDS: Readonly<Record<string, string>> = {
  NAME_TAKEN: 'Name taken',
  NAME_INVALID: 'Name not allowed',
  PARENT_NOT_WRITABLE: 'Folder not writable',
  EXAMPLE_NOT_FOUND: 'Example not found',
  NEEDS_NETWORK: 'Needs network',
  COPY_FAILED: 'Copy failed',
};

/** The one line of the provisioning slot: a status word, with the host's detail on the pointer. */
export function provisionLine(
  progress: ProvisionProgress,
): {readonly text: string; readonly title?: string; readonly failed: boolean} | null {
  switch (progress._tag) {
    case 'Idle':
      return null;
    case 'Working':
      return {text: 'Copying', failed: false};
    case 'Done':
      return {text: 'Copied', title: progress.repository, failed: false};
    case 'Refused':
      return {
        text: REFUSAL_WORDS[progress.reason] ?? 'Not copied',
        title: progress.detail,
        failed: true,
      };
    case 'Failed':
      return {text: 'Not copied', title: progress.detail, failed: true};
    default:
      progress satisfies never;
      return null;
  }
}
