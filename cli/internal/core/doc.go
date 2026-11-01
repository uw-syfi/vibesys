// Package core holds the domain types and the pure rules of vibesys-cli. Everything a decision
// needs arrives as an argument and everything it decides is returned, so every rule here is
// tested without a store, a clock or a process.
//
// The rules, by file:
//
//   - key.go: NewJobKey, the digest that names a job's inputs (kind, commit, task version,
//     environment fingerprint, settings kind, schema version). It is canonical and stable; a golden
//     test pins it.
//   - version.go: SchemaVersion and ProtocolVersion, one definition each, and CheckCompatibility
//     for the handshake.
//   - transition.go: the job state machine (Job.Apply, JobState.CanMoveTo) and the checks that a
//     transition carries the result and measurement its target state needs.
//   - cache.go: PlanCache, which says whether a submission reuses saved results, tops them up or
//     runs, and CanJoin, which says whether it joins a job already in flight.
//   - compare.go: Compare, the verdict on two sample sets, and compare_commits.go: CompareCommits,
//     the same over the saved measurements of two commits.
//   - promote.go: DecidePromotion, and current.go: CurrentResultOf, the number `result` reports.
//   - environment.go: ChooseEnvironment, the rule from a task's backend and setup to a run
//     environment.
//
// It imports only the standard library and performs no I/O, reads no clock and starts no
// goroutines. Time arrives as a value. These types are one of three separate models: the
// generated contract types (gen/) and the on-disk encoding (internal/store/filestore) are the
// others, and internal/wire translates between core and gen. None is another's representation.
package core
