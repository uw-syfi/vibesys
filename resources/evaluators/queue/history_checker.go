package main

import (
	"encoding/binary"
	"hash/fnv"
	"time"

	"github.com/anishathalye/porcupine"
)

// defaultCheckBudget bounds one linearizability decision.
//
// Porcupine's search is worst-case exponential in the number of overlapping
// operations, so a concurrent history is not guaranteed to be decidable in any
// particular time. Deciding a real gate history takes milliseconds, but some
// interleavings of an MPMC history are pathological and run for minutes. An
// unbounded check turns those into a hang that the surrounding command timeout
// kills without a diagnostic.
//
// 20s is orders of magnitude above a normal decision, and it keeps the
// benchmark gate (four boundary histories plus one concurrent trial, so five
// decisions) inside the framework's 300s benchmark timeout with room for the
// measured run.
const defaultCheckBudget = 20 * time.Second

// hashValues hashes a sequence for a model's Hash. Porcupine keys its memo on the set of linearized
// operations alone when a model has no Hash, so every state reached with the same set lands in one
// bucket and each lookup compares against all of them. A hash keeps lookups constant, which is what
// keeps a heavily overlapped history inside the budget.
func hashValues(values ...uint64) uint64 {
	hash := fnv.New64a()
	var word [8]byte
	for _, value := range values {
		binary.LittleEndian.PutUint64(word[:], value)
		_, _ = hash.Write(word[:])
	}
	return hash.Sum64()
}

// checkScenarioHistory decides history against the scenario's correctness
// contract within budget. The verdict is tri-state: porcupine.Unknown means the
// budget expired before the search finished, which is neither a pass nor a
// proven violation.
func checkScenarioHistory(
	s scenario,
	capacity int,
	history []recordedOperation,
	budget time.Duration,
) porcupine.CheckResult {
	switch s {
	case scenarioSPSC, scenarioMPSC:
		return checkExactFIFOHistory(capacity, history, budget)
	case scenarioMPMC:
		return checkReservationAwareFIFOHistory(capacity, history, budget)
	default:
		return porcupine.Illegal
	}
}

func correctnessContract(s scenario) string {
	if s == scenarioMPMC {
		return "reservation-aware bounded FIFO"
	}
	return "linearizable bounded FIFO"
}
