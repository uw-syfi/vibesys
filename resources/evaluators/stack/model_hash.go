package main

import (
	"encoding/binary"
	"hash/fnv"
)

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
