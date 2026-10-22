/** UUID generation is provided by every supported runtime without requiring DOM or Node types. */
interface Crypto {
  randomUUID(): string;
}

declare var crypto: Crypto;
