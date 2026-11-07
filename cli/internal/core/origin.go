package core

import "time"

// RecordOrigin is who made a record an agent writes through the CLI, and when. The daemon fills it
// from the identity the call carried (VIBESYS_RUN and VIBESYS_AGENT, which the caller states and
// nothing authenticates) and its own clock; a request never carries one.
type RecordOrigin struct {
	// Run and Agent are empty for a call from outside a run.
	Run   RunID
	Agent string
	At    time.Time
}

// MaxOriginBytes is the longest run id or agent name a record's origin holds.
const MaxOriginBytes = 256

// Check says whether the origin has the time it was made and names within their limits. The error
// is a *Failure of kind FailureInvalidInput that names the field, with field as the start of its
// path.
func (o RecordOrigin) Check(field string) error {
	switch {
	case o.At.IsZero():
		return failf(FailureInvalidInput, "%s.at: a record has the time it was made", field)
	case len(o.Run) > MaxOriginBytes:
		return failf(FailureInvalidInput, "%s.run: %d bytes is over the limit of %d", field, len(o.Run), MaxOriginBytes)
	case len(o.Agent) > MaxOriginBytes:
		return failf(FailureInvalidInput, "%s.agent: %d bytes is over the limit of %d", field, len(o.Agent), MaxOriginBytes)
	}
	return nil
}

// Same says whether two origins name the same run and agent at the same instant.
func (o RecordOrigin) Same(other RecordOrigin) bool {
	return o.Run == other.Run && o.Agent == other.Agent && o.At.Equal(other.At)
}
