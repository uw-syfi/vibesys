/** Serves an ended run's summary, then only the requested step span's events. */
export interface FakeSummarySpanSeed {
  readonly summary: unknown;
  readonly events: readonly {readonly sequence?: number; readonly span_id?: string | null}[];
}

export function installFakeSummarySpan(seed: FakeSummarySpanSeed): void {
  const calls: string[] = [];
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(target.vibesysWelcome, {
    readSummary: () => {
      calls.push('summary');
      return ok({kind: 'summary', summary: seed.summary});
    },
    readEvents: () => {
      calls.push('history');
      return ok({kind: 'unsupported'});
    },
    readSpan: (
      _host: string,
      _project: string,
      _run: string,
      spanId: string,
      from: number,
      max: number,
    ) => {
      calls.push(`span:${spanId}`);
      const selected = seed.events.filter(
        event => event.span_id === spanId && (event.sequence ?? 0) >= from,
      );
      const page = selected.slice(0, max);
      const next = (page.at(-1)?.sequence ?? from - 1) + 1;
      return ok({kind: 'page', events: page, next, complete: page.length === selected.length});
    },
  });
  Object.assign(target.fakeHost, {historyCalls: calls});
}
