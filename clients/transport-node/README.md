# VibeSys Node transport

`@vibesys/transport-node` is the Node-only side of the transport to the Python
backend server: `ServerClient`, the Unix-domain-socket client the TUI runs. It
builds on [`@vibesys/transport`](../transport/README.md) (the control channel,
`NewlineFramer`, and request policy) and adds only what needs `node:net`.

Its one entry is `node`: only `node` entries (the TUI, Electron main, and
features' `./main`) may import it, so it never reaches a renderer or browser
bundle (`docs/contributing/clients-architecture.md`).

From the repository root:

```bash
pnpm --dir clients/transport-node check
pnpm --dir clients/transport-node test
pnpm --dir clients/transport-node build
```
