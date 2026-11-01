package core

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"maps"
	"slices"
)

// keyDomain separates job keys from any other digest made of the same fields. Changing how the
// fields are encoded changes keys, so it needs a SchemaVersion bump, which retires every cached
// result on purpose.
const keyDomain = "vibesys-cli/job-key"

// NewJobKey is the cache key of a job: the SHA-256 of a canonical encoding of the inputs, as 64
// lowercase hexadecimal characters.
//
// The encoding is canonical: it does not depend on map order, on how a caller built the inputs,
// or on this process. Every string is length prefixed and every field has a fixed place, so no two
// different inputs encode alike. A golden test pins the digest of one input.
//
// What is in the key is what the result is a result of: the job kind, the exact commit, the task
// version, the environment fingerprint and the schema version. Settings take part through their
// kind and the fields that change what is computed, and there are none yet. MeasureSettings
// leaves out both of its fields on purpose. Samples is a quantity the cache tops up (PlanCache),
// so results taken for different counts must share a key; Recheck changes how a check is
// obtained, never what is measured. Notes, plans and the caller never take part.
//
// It fails with a *Failure of kind FailureInvalidInput that names the field for a command job,
// which has no key; settings that are missing or of another kind; a commit that is not 40
// lowercase hexadecimal characters; an empty task version or schema version; or an environment
// that is not a fully pinned host or docker fingerprint.
func NewJobKey(in KeyInputs) (JobKey, error) {
	if err := validateKeyInputs(in); err != nil {
		return "", err
	}
	var e encoder
	e.string(keyDomain)
	e.number(in.SchemaVersion)
	e.string(string(in.Kind))
	e.string(string(in.Commit))
	e.string(in.TaskVersion)
	e.fingerprint(in.Environment)
	e.string(string(in.Settings.Kind()))
	// Key-relevant settings fields would follow here. See the doc comment above for why none do.
	sum := sha256.Sum256(e.bytes)
	return JobKey(hex.EncodeToString(sum[:])), nil
}

func validateKeyInputs(in KeyInputs) error {
	switch in.Kind {
	case JobCheck, JobMeasure:
	case JobCommand:
		return failf(FailureInvalidInput, "kind: command jobs are never cached, so they have no key")
	default:
		return failf(FailureInvalidInput, "kind: %q is not a job kind", in.Kind)
	}
	if in.Settings == nil {
		return failf(FailureInvalidInput, "settings: missing")
	}
	if got := in.Settings.Kind(); got != in.Kind {
		return failf(FailureInvalidInput, "settings: %s settings on a %s job", got, in.Kind)
	}
	if !in.Commit.Valid() {
		return failf(FailureInvalidInput, "commit: %q is not 40 lowercase hexadecimal characters", in.Commit)
	}
	if in.TaskVersion == "" {
		return failf(FailureInvalidInput, "task version: empty")
	}
	if in.SchemaVersion == 0 {
		return failf(FailureInvalidInput, "schema version: zero")
	}
	return in.Environment.validatePinned()
}

// encoder builds the canonical byte string a key is the digest of.
type encoder struct {
	bytes []byte
}

func (e *encoder) number(n uint32) {
	e.bytes = binary.BigEndian.AppendUint32(e.bytes, n)
}

func (e *encoder) string(s string) {
	e.bytes = binary.AppendUvarint(e.bytes, uint64(len(s)))
	e.bytes = append(e.bytes, s...)
}

func (e *encoder) fingerprint(f Fingerprint) {
	e.string(string(f.Kind))
	e.string(string(f.Backend))
	e.string(f.ImageDigest)
	e.number(uint32(len(f.Facts)))
	for _, name := range slices.Sorted(maps.Keys(f.Facts)) {
		e.string(name)
		e.string(f.Facts[name])
	}
}
