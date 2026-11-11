/**
 * `phrase` with "yet" while the run is live and more may still come ("no ideas yet"), and bare once it is
 * over: "yet" promises something an ended run cannot keep.
 */
export const yet = (phrase: string, over: boolean): string => (over ? phrase : `${phrase} yet`);
