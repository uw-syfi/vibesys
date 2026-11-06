// Command oracle is the kv-store example's accuracy oracle, on the Go SDK's checker (go/check):
// it checks a captured history of the workload in ../generator against the store's semantics
// (package kvcheck, which the Go task also runs in-process).
//
// bench.py's accuracy check (microservices.Accuracy) runs it on the history it captured:
//
//	oracle --history DIR [--out /vibesys/artifacts/accuracy]
//
// and it exits 0 (PASS), 1 (FAIL) or 2 (UNKNOWN). Run with KV_STORE_SCALE=test for the
// end-to-end test's scale, as the generator.
package main

import (
	"os"

	"example.com/kv-store/oracle/kvcheck"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
)

func main() {
	s := kvcheck.Example
	if os.Getenv("KV_STORE_SCALE") == "test" {
		s = kvcheck.Test
	}
	check.Main(kvcheck.Oracle(s))
}
