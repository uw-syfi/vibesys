package wire

import (
	"fmt"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// JobOutputs returns the contract's message for a command job's outputs, nil for none.
func JobOutputs(o core.Outputs) *cliv1.JobOutputs {
	switch o := o.(type) {
	case core.OutputsKept:
		files := make([]*cliv1.OutputFile, 0, len(o.Files))
		for _, f := range o.Files {
			files = append(files, &cliv1.OutputFile{Path: f.Path, Size: f.Size, Sha256: f.SHA256})
		}
		return &cliv1.JobOutputs{Record: &cliv1.JobOutputs_Kept{Kept: &cliv1.OutputFiles{Files: files}}}
	case core.OutputsRefused:
		return &cliv1.JobOutputs{Record: &cliv1.JobOutputs_Refused{Refused: o.Reason}}
	}
	return nil
}

// ToJobOutputs returns the domain record for the contract's message, checked as a job holds it
// (core.OutputsKept.Check); nil for nil.
func ToJobOutputs(field string, m *cliv1.JobOutputs) (core.Outputs, error) {
	if m == nil {
		return nil, nil
	}
	if err := protovalidate.Validate(m); err != nil {
		return nil, invalid("%s: %v", field, err)
	}
	var out core.Outputs
	switch r := m.GetRecord().(type) {
	case *cliv1.JobOutputs_Kept:
		kept := core.OutputsKept{}
		for _, f := range r.Kept.GetFiles() {
			kept.Files = append(kept.Files, core.OutputFile{Path: f.GetPath(), Size: f.GetSize(), SHA256: f.GetSha256()})
		}
		out = kept
	case *cliv1.JobOutputs_Refused:
		out = core.OutputsRefused{Reason: r.Refused}
	default:
		return nil, invalid("%s: neither kept nor refused", field)
	}
	if err := core.CheckOutputs(out); err != nil {
		return nil, fmt.Errorf("%s: %w", field, err)
	}
	return out, nil
}
