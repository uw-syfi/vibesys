import {LatestQueryOwner} from './latest-query-owner.js';

interface DesignQueryOwnerConfig<T> {
  readonly request: () => Promise<T>;
  readonly publish: (value: T) => void;
  readonly report: (error: unknown) => void;
}

/** Add pane-error intent to the shared latest-query ownership mechanism. */
export class DesignQueryOwner<T> {
  #paneFailureWanted = false;
  readonly #owner: LatestQueryOwner<T>;

  constructor(config: DesignQueryOwnerConfig<T>) {
    this.#owner = new LatestQueryOwner({
      request: config.request,
      publish: value => {
        this.#paneFailureWanted = false;
        config.publish(value);
      },
      report: error => {
        const report = this.#paneFailureWanted;
        this.#paneFailureWanted = false;
        if (report) config.report(error);
      },
    });
  }

  refresh(reportPaneFailure = false): Promise<void> {
    if (reportPaneFailure) this.#paneFailureWanted = true;
    return this.#owner.refresh();
  }

  stop(): void {
    this.#paneFailureWanted = false;
    this.#owner.stop();
  }
}
