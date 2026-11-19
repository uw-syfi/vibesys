package core_test

import (
	"fmt"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// outputEntries is what a command may leave: mostly regular files of any size, now and then many of
// them, a huge one, a link, a device or a path a kept file cannot have.
func outputEntries(r *rand.Rand) []core.OutputEntry {
	n := r.IntN(6)
	if r.IntN(8) == 0 {
		n = core.MaxOutputFiles - 2 + r.IntN(5)
	}
	var out []core.OutputEntry
	for i := range n {
		e := core.OutputEntry{Path: fmt.Sprintf("d%d/f%d.json", i%3, i), Kind: core.OutputRegular, Size: uint64(r.IntN(1 << 10))}
		switch r.IntN(40) {
		case 0:
			e.Size = core.MaxOutputBytes/2 + uint64(r.IntN(core.MaxOutputBytes))
		case 1:
			e.Kind, e.Size = core.OutputSymlink, 0
		case 2:
			e.Kind, e.Size = core.OutputOther, 0
		case 3:
			e.Path = strings.Repeat("x", core.MaxOutputPathBytes+1)
		case 4:
			e.Path = "bad\xffname"
		}
		out = append(out, e)
	}
	return out
}

// A job keeps what it left exactly when it is within both bounds and every entry is a regular
// file with a path a kept file may have; a refusal past a bound names the bound and what was left,
// counted in full.
func TestOutputsAreKeptExactlyWithinTheBounds(t *testing.T) {
	r := newRand(41)
	for range properties {
		entries := outputEntries(r)
		var bytes uint64
		allRegular, allPaths := true, true
		for _, e := range entries {
			bytes += e.Size
			allRegular = allRegular && e.Kind == core.OutputRegular
			allPaths = allPaths && core.CheckOutputPath(e.Path) == nil
		}
		tooMany, tooBig := len(entries) > core.MaxOutputFiles, bytes > core.MaxOutputBytes
		refused := core.RefuseOutputs(entries)
		if want := tooMany || tooBig || !allRegular || !allPaths; (refused != nil) != want {
			t.Fatalf("%d entries of %d bytes (regular %v, paths %v): refused = %+v, want refused %v", len(entries), bytes, allRegular, allPaths, refused, want)
		}
		if refused == nil {
			continue
		}
		if err := refused.Check(); err != nil {
			t.Fatalf("a refusal says nothing: %v", err)
		}
		if tooMany && (!strings.Contains(refused.Reason, strconv.Itoa(len(entries))+" files") || !strings.Contains(refused.Reason, strconv.Itoa(core.MaxOutputFiles))) {
			t.Fatalf("a refusal of %d files does not name the count and the bound: %s", len(entries), refused.Reason)
		}
		if tooBig && (!strings.Contains(refused.Reason, fmt.Sprintf("(%d bytes)", bytes)) || !strings.Contains(refused.Reason, fmt.Sprintf("(%d bytes)", core.MaxOutputBytes))) {
			t.Fatalf("a refusal of %d bytes does not name the size and the bound: %s", bytes, refused.Reason)
		}
	}
}

func TestAnOutputPathStaysInsideItsDirectory(t *testing.T) {
	for _, p := range []string{"a", "a/b.json", ".hidden", "a/.b/c", "日本語.txt", "a..b"} {
		if err := core.CheckOutputPath(p); err != nil {
			t.Errorf("%q: %v", p, err)
		}
	}
	for _, p := range []string{"", ".", "..", "../a", "a/../b", "/abs", "a//b", "a/", "./a", "a/.", `a\b`, "nul\x00", "bad\xff", strings.Repeat("y", core.MaxOutputPathBytes+1)} {
		if err := core.CheckOutputPath(p); err == nil {
			t.Errorf("%q is accepted", p)
		}
	}
}

func TestByteSizeIsExact(t *testing.T) {
	for n, want := range map[uint64]string{
		0: "0 bytes", 1: "1 byte", 1023: "1023 bytes", 1024: "1 KiB (1024 bytes)", 1536: "1.5 KiB (1536 bytes)",
		core.MaxOutputBytes: "256 MiB (268435456 bytes)", 3 << 30: "3 GiB (3221225472 bytes)",
	} {
		if got := core.ByteSize(n); got != want {
			t.Errorf("ByteSize(%d) = %q, want %q", n, got, want)
		}
	}
}

var keptOne = core.OutputsKept{Files: []core.OutputFile{{Path: "t.json", Size: 2, SHA256: strings.Repeat("a", 64)}}}

// A job records outputs only on the move of a command job that declared them from running to a
// final state, and keeps them as given; every other move with outputs is refused.
func TestOnlyTheEndOfACommandThatDeclaredOutputsRecordsThem(t *testing.T) {
	for _, kind := range jobKinds {
		for _, declared := range []bool{false, true} {
			for _, from := range core.JobStates() {
				for _, to := range core.JobStates() {
					j := newJob(kind, from)
					if c, ok := j.Spec.Settings.(core.CommandSettings); ok {
						c.Outputs = declared
						j.Spec.Settings = c
					}
					tr := moveTo(j, to)
					if !from.CanMoveTo(to) || tr.To == core.JobQueued || tr.Result != nil || tr.Environment != nil {
						continue
					}
					tr.Outputs = keptOne
					out, err := j.Apply(tr)
					want := kind == core.JobCommand && declared && from == core.JobRunning && to.Final()
					if (err == nil) != want {
						t.Fatalf("%s job (declared %v) %s to %s with outputs: err = %v, want applied %v", kind, declared, from, to, err, want)
					}
					if want && fmt.Sprint(out.Outputs) != fmt.Sprint(keptOne) {
						t.Fatalf("the job keeps %+v, want %+v", out.Outputs, keptOne)
					}
				}
			}
		}
	}
}

// A record a job cannot hold is refused even on the right move.
func TestARecordOutsideTheRulesIsNotRecorded(t *testing.T) {
	j := newJob(core.JobCommand, core.JobRunning)
	j.Spec.Settings = core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}, Outputs: true}
	sha := strings.Repeat("b", 64)
	for name, o := range map[string]core.Outputs{
		"empty refusal":  core.OutputsRefused{},
		"escaping path":  core.OutputsKept{Files: []core.OutputFile{{Path: "../x", SHA256: sha}}},
		"not a digest":   core.OutputsKept{Files: []core.OutputFile{{Path: "x", SHA256: "sha256:x"}}},
		"out of order":   core.OutputsKept{Files: []core.OutputFile{{Path: "b", SHA256: sha}, {Path: "a", SHA256: sha}}},
		"twice":          core.OutputsKept{Files: []core.OutputFile{{Path: "a", SHA256: sha}, {Path: "a", SHA256: sha}}},
		"past the bytes": core.OutputsKept{Files: []core.OutputFile{{Path: "a", Size: core.MaxOutputBytes + 1, SHA256: sha}}},
	} {
		tr := moveTo(j, core.JobFailed)
		tr.Outputs = o
		if _, err := j.Apply(tr); failureKind(err) != core.FailureInvalidInput {
			t.Errorf("%s: %v, want an invalid_input failure", name, err)
		}
	}
}
