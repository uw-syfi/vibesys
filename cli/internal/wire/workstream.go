package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// workstreamStatuses pairs each domain status with its contract value. Both directions read this
// table, so they cannot disagree.
var workstreamStatuses = []struct {
	domain core.WorkstreamStatus
	wire   cliv1.WorkstreamStatus
}{
	{core.WorkstreamActive, cliv1.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE},
	{core.WorkstreamPaused, cliv1.WorkstreamStatus_WORKSTREAM_STATUS_PAUSED},
	{core.WorkstreamClosed, cliv1.WorkstreamStatus_WORKSTREAM_STATUS_CLOSED},
}

// WorkstreamStatus returns the contract's value for a status, or unspecified for a status it does
// not name.
func WorkstreamStatus(s core.WorkstreamStatus) cliv1.WorkstreamStatus {
	for _, w := range workstreamStatuses {
		if w.domain == s {
			return w.wire
		}
	}
	return cliv1.WorkstreamStatus_WORKSTREAM_STATUS_UNSPECIFIED
}

// ToWorkstreamStatus returns the domain status for the contract's value, or invalid_input naming
// field.
func ToWorkstreamStatus(field string, s cliv1.WorkstreamStatus) (core.WorkstreamStatus, error) {
	for _, w := range workstreamStatuses {
		if w.wire == s {
			return w.domain, nil
		}
	}
	return "", invalid("%s: %s is not a workstream status", field, s)
}

// Workstream returns the contract's message for a workstream.
func Workstream(w core.Workstream) *cliv1.Workstream {
	return &cliv1.Workstream{
		Number: uint32(w.Number), Title: w.Title, Status: WorkstreamStatus(w.Status), Note: w.Note,
		Origin: RecordOrigin(w.Origin), UpdatedAt: timestamp(w.UpdatedAt),
	}
}

// Workstreams returns the contract's messages for workstreams, in order.
func Workstreams(workstreams []core.Workstream) []*cliv1.Workstream {
	out := make([]*cliv1.Workstream, 0, len(workstreams))
	for _, w := range workstreams {
		out = append(out, Workstream(w))
	}
	return out
}

// ToWorkstream returns the domain workstream for the contract's message, or invalid_input naming
// field.
func ToWorkstream(field string, w *cliv1.Workstream) (core.Workstream, error) {
	status, err := ToWorkstreamStatus(field+".status", w.GetStatus())
	if err != nil {
		return core.Workstream{}, err
	}
	return core.Workstream{
		Number: core.WorkstreamNumber(w.GetNumber()), Title: w.GetTitle(), Status: status, Note: w.GetNote(),
		Origin: toRecordOrigin(w.GetOrigin()), UpdatedAt: toTime(w.GetUpdatedAt()),
	}, nil
}

// ToWorkstreamFilter returns the domain filter for a ListWorkstreams request.
func ToWorkstreamFilter(r *cliv1.ListWorkstreamsRequest) (core.WorkstreamFilter, error) {
	var filter core.WorkstreamFilter
	for _, s := range r.GetStatuses() {
		status, err := ToWorkstreamStatus("statuses", s)
		if err != nil {
			return core.WorkstreamFilter{}, err
		}
		filter.Statuses = append(filter.Statuses, status)
	}
	return filter, nil
}
