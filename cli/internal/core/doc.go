// Package core holds the domain types and, in later changes, the pure rules of vibesys-cli: cache
// keys, verdicts, promotion checks and the job state machine.
//
// It imports nothing from this module and performs no I/O, reads no clock and starts no
// goroutines. Time arrives as a value. These types are one of three separate models: the
// generated contract types (gen/) and the on-disk encoding (internal/store/filestore) are the
// others, and internal/wire translates between core and gen. None is another's representation.
package core
