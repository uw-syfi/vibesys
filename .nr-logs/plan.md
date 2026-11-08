# Item C plan (contracts and scaffolding)
Generator: .nr-logs/gen.sh (sets PATH/GOFLAGS). Each step: gen, go build/test touched, format+lint, commit, push.
1. Shared vocab + cli protos: AcceleratorVendor/Model/Need/Offer (environment.proto), typed TaskCatalogue + TaskCard (task.proto), cli placement.proto, DaemonService.DescribeTask/ListPlacements. Verify: 49 example tasks decode (cli task tests), buf lint/breaking, gen clean.
2. Orchestrator protos: catalogue.proto, eval_place.proto, provision.proto, setup.proto; Run/StartRun/RunSummary/Command/Refusal/Receipt/RunChange additions; service rpcs; desktop settings field. Verify: buf, gen, go build.
3. Go scaffolding: wire/core.model field mapping + all-fields property test, enum arms, Unimplemented handlers (daemon + CLI), ports + Fakes with wiring tests.
4. Desktop: fake daemon answers new rpcs (new files), gateway-calls allow-list, settings field plumbing; vitest touched files.
5. Cleanup pass, doctor/repoctl test, delete .nr-logs, PR.
