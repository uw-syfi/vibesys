import type {SteerDeliveryFact} from '@vibesys/run-state';
import {decodeRecords} from '@vibesys/runs/chat';
import {useEffect, useState} from 'react';
import {emptySteerFold, foldSteer, steerDeliveries} from '../../state/index.js';

/** The run's one reader hands every consumer its batches of records, and none reads the log itself. */
export type RunRecords = (listener: (records: readonly Uint8Array[]) => void) => () => void;

/** The steer delivery steps the run's records report; empty until a record arrives. */
export function useSteerDeliveries(
  records: RunRecords,
  enabled: boolean,
): readonly SteerDeliveryFact[] {
  const [facts, setFacts] = useState<readonly SteerDeliveryFact[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let fold = emptySteerFold;
    return records(batch => {
      fold = foldSteer(fold, decodeRecords(batch));
      setFacts(steerDeliveries(fold));
    });
  }, [records, enabled]);
  return facts;
}
