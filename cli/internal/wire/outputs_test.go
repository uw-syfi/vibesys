package wire_test

import (
	"fmt"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The contract's bounds on what a job keeps are core's: a record at each bound is read, and one past
// it is refused by both, so neither can hold a record the other refuses.
func TestTheContractBoundsTheOutputsAsCoreDoes(t *testing.T) {
	sha := strings.Repeat("c", 64)
	many := func(n int) core.OutputsKept {
		var k core.OutputsKept
		for i := range n {
			k.Files = append(k.Files, core.OutputFile{Path: fmt.Sprintf("f%05d", i), SHA256: sha})
		}
		return k
	}
	sized := func(size uint64) core.OutputsKept {
		return core.OutputsKept{Files: []core.OutputFile{{Path: "big", Size: size, SHA256: sha}}}
	}
	long := func(n int) core.OutputsKept {
		return core.OutputsKept{Files: []core.OutputFile{{Path: strings.Repeat("p", n), SHA256: sha}}}
	}
	for name, c := range map[string]struct {
		kept core.OutputsKept
		ok   bool
	}{
		"the most files":       {many(core.MaxOutputFiles), true},
		"one file too many":    {many(core.MaxOutputFiles + 1), false},
		"the most bytes":       {sized(core.MaxOutputBytes), true},
		"one byte too many":    {sized(core.MaxOutputBytes + 1), false},
		"the longest path":     {long(core.MaxOutputPathBytes), true},
		"a path a byte longer": {long(core.MaxOutputPathBytes + 1), false},
	} {
		_, err := wire.ToJobOutputs("outputs", wire.JobOutputs(c.kept))
		if (err == nil) != c.ok || (c.kept.Check() == nil) != c.ok {
			t.Errorf("%s: contract says %v, core says %v; want accepted %v", name, err, c.kept.Check(), c.ok)
		}
		if err != nil && !strings.Contains(err.Error(), "outputs") {
			t.Errorf("%s: the refusal does not name the field: %v", name, err)
		}
	}
}
