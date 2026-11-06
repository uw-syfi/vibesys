package check

import "time"

// Options bound a check. A zero field takes its default (DefaultOptions).
type Options struct {
	// PartitionTimeout bounds one partition's search; past it the partition is undecided.
	PartitionTimeout time.Duration
	// TotalTimeout bounds the whole check, from projection to the last search: partitions not
	// started by then are undecided (status skipped). Explaining failures has its own budget.
	TotalTimeout time.Duration
	// MaxSearchSteps bounds one partition's search in model steps, a budget that does not depend
	// on the machine's speed. Negative: no bound (the default).
	MaxSearchSteps int64
	// MemoryBytes bounds the checker's heap. Above it the search that has taken the most steps is
	// aborted (undecided, status memory), one at a time; when all partitions are done, those are
	// retried alone.
	MemoryBytes int64
	// Workers is how many partitions are searched at once (default GOMAXPROCS).
	Workers int
	// MaxReadsPerKey keeps at most this many reads per key, a uniform sample drawn from Seed;
	// writes are always kept. Negative: keep every read.
	MaxReadsPerKey int
	// MaxOpsPerPartition: a partition with more ops than this after sampling is not searched
	// (undecided, status too-large).
	MaxOpsPerPartition int
	// MaxLostShare is the largest share of pieces with an unknown outcome (Piece.Unknown: lost
	// responses and 5xx alike) a spec accepts; above it the check fails, since at the check's
	// fixed low load they are the candidate's fault. Negative: no limit.
	MaxLostShare float64
	// OverlapWarning: a partition with more ops in flight at once than this gets a warning, since
	// a search that must reject a history costs about 2^overlap per operation.
	OverlapWarning int
	// ExplainTimeout bounds the search for each failing partition's first violation.
	ExplainTimeout time.Duration
	// MaxExplained is how many failing partitions per spec are explained.
	MaxExplained int
	// MaxTimelineOps: a failing prefix with more ops than this gets no HTML timeline.
	MaxTimelineOps int
	// Seed seeds the read sample.
	Seed int64
	// HeapBytes measures the heap the memory budget bounds; nil measures the Go runtime's heap
	// objects. A test may count something else.
	HeapBytes func() int64
}

// DefaultOptions are what a check gets for the fields left zero.
var DefaultOptions = Options{
	PartitionTimeout:   60 * time.Second,
	TotalTimeout:       10 * time.Minute,
	MaxSearchSteps:     -1,
	MemoryBytes:        2 << 30,
	MaxReadsPerKey:     5000,
	MaxOpsPerPartition: 50000,
	MaxLostShare:       0.01,
	OverlapWarning:     12,
	ExplainTimeout:     60 * time.Second,
	MaxExplained:       3,
	MaxTimelineOps:     3000,
	Seed:               1,
}

func (o Options) withDefaults() Options {
	d := DefaultOptions
	set := func(v *int, def int) {
		if *v == 0 {
			*v = def
		}
	}
	if o.PartitionTimeout == 0 {
		o.PartitionTimeout = d.PartitionTimeout
	}
	if o.TotalTimeout == 0 {
		o.TotalTimeout = d.TotalTimeout
	}
	if o.MaxSearchSteps == 0 {
		o.MaxSearchSteps = d.MaxSearchSteps
	}
	if o.MemoryBytes == 0 {
		o.MemoryBytes = d.MemoryBytes
	}
	set(&o.MaxReadsPerKey, d.MaxReadsPerKey)
	set(&o.MaxOpsPerPartition, d.MaxOpsPerPartition)
	if o.MaxLostShare == 0 {
		o.MaxLostShare = d.MaxLostShare
	}
	set(&o.OverlapWarning, d.OverlapWarning)
	if o.ExplainTimeout == 0 {
		o.ExplainTimeout = d.ExplainTimeout
	}
	set(&o.MaxExplained, d.MaxExplained)
	set(&o.MaxTimelineOps, d.MaxTimelineOps)
	if o.Seed == 0 {
		o.Seed = d.Seed
	}
	if o.HeapBytes == nil {
		o.HeapBytes = heapObjects
	}
	return o
}
