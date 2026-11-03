/**
 * The words of the statement a run that ended before any of its work began shows in place of its
 * charts, time bar and faces (`unstartedRun`): what happened, and what each way on does. The run's
 * reason is its own, read from its record.
 */
export const UNSTARTED_WORDS = {
  title: 'Ended during setup, before trying any idea',
  runAgain: 'Run again',
  runAgainHint: 'Start a new run from this one’s settings, which you can change first',
  viewLogs: 'View logs',
  viewLogsHint: 'The run’s log, with each setup step it took',
} as const;
