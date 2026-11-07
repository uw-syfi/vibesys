package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// RecordOrigin returns the contract's message for who made a record and when.
func RecordOrigin(o core.RecordOrigin) *cliv1.RecordOrigin {
	return &cliv1.RecordOrigin{RunId: string(o.Run), Agent: o.Agent, At: timestamp(o.At)}
}

// toRecordOrigin returns the domain origin for the contract's message.
func toRecordOrigin(o *cliv1.RecordOrigin) core.RecordOrigin {
	return core.RecordOrigin{Run: core.RunID(o.GetRunId()), Agent: o.GetAgent(), At: toTime(o.GetAt())}
}

// Bound returns the contract's message for a declared bound.
func Bound(b core.Bound) *cliv1.ObjectiveBound {
	return &cliv1.ObjectiveBound{
		Seq: b.Seq, Origin: RecordOrigin(b.Origin), Objective: b.Objective, Direction: direction(b.Direction),
		Value: b.Value, Unit: b.Unit, Note: b.Note,
	}
}

// Bounds returns the contract's messages for bounds, in order.
func Bounds(bounds []core.Bound) []*cliv1.ObjectiveBound {
	out := make([]*cliv1.ObjectiveBound, 0, len(bounds))
	for _, b := range bounds {
		out = append(out, Bound(b))
	}
	return out
}

// ToBound returns the domain bound for the contract's message, or invalid_input naming the field.
func ToBound(field string, b *cliv1.ObjectiveBound) (core.Bound, error) {
	d, err := toDirection(field+".direction", b.GetDirection())
	if err != nil {
		return core.Bound{}, err
	}
	return core.Bound{Seq: b.GetSeq(), NewBound: core.NewBound{
		Origin: toRecordOrigin(b.GetOrigin()), Objective: b.GetObjective(), Direction: d,
		Value: b.GetValue(), Unit: b.GetUnit(), Note: b.GetNote(),
	}}, nil
}

// StandingBounds returns the bounds that stand, one per objective in the order of the log, from a
// log as the contract has it: the newest declaration of each objective (core.StandingBounds). A
// bound the contract does not allow is invalid input.
func StandingBounds(bounds []*cliv1.ObjectiveBound) ([]*cliv1.ObjectiveBound, error) {
	log := make([]core.Bound, 0, len(bounds))
	for _, b := range bounds {
		d, err := ToBound("bounds", b)
		if err != nil {
			return nil, err
		}
		log = append(log, d)
	}
	return Bounds(core.StandingBounds(log)), nil
}
