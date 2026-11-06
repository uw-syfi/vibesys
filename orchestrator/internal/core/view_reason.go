package core

// ViewReason says whether a run's retained search view can currently be refreshed, and if not,
// why. It is the one vocabulary for that: the link to the job service reports its failures in it,
// the search view carries it, and the wire enum UnreadableReason is its only other spelling, so a
// terminal cause cannot be lost between layers.
type ViewReason uint8

const (
	// Readable means the view is current.
	Readable ViewReason = iota
	NoDaemon
	StartFailed
	CLINotInstalled
	ClientTooOld
	DaemonTooOld
	StreamLost
	// MessageTooLarge means the job service sent a message past the link's size bound, which
	// retrying the same read cannot fix.
	MessageTooLarge
	StoreOwnedElsewhere
	StoreAway
	// Connecting means the first read is still in flight.
	Connecting
)
