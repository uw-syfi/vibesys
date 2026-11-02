// Package wire translates between the domain types of internal/core (and internal/lifecycle) and
// the generated contract types of gen/. It is the only package that imports both, so the three
// models, domain, contract and on-disk encoding, stay separate.
//
// Translation toward the domain validates: a value the contract can say and the domain cannot
// hold, such as an unspecified enum or a commit that is not 40 hexadecimal characters, is a
// *core.Failure of kind invalid_input naming the field. Translation toward the contract cannot
// fail for a valid domain value.
//
//   - enums.go: the closed sets (states, kinds, environments, backends, dispositions).
//   - job.go: jobs, specs, notes, fingerprints and the call context.
//   - failure.go: Failure, and the gRPC status that carries one.
//   - supervisor.go: Launch and Outcome, what the daemon and a supervisor exchange.
//   - versions.go: the version handshake.
//   - await.go: the wait rule over the contract's job messages.
package wire
