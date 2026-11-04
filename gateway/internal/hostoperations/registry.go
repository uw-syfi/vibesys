package hostoperations

import "time"

// FrameTiming is how often the gateway speaks on any subscription and how long a
// client waits for it. The descriptor states both, and every language reads them
// from its generated table.
type FrameTiming struct {
	Heartbeat, Lease time.Duration
}

// TopicPolicy is one subscription topic's fixed cadence and bounds. Zero Cadence
// means event driven; zero StaleAfter means the frame lease alone decides; zero SourceLease means
// the source is held to no sign of life.
type TopicPolicy struct {
	Cadence, StaleAfter, FirstFrame, MinInterval, SourceLease time.Duration
	MaxKeys, MaxValueBytes                                    int
}
