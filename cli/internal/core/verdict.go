package core

// VerdictKind is the outcome of comparing a commit's samples with a base's.
type VerdictKind string

// The verdict kinds.
const (
	Better       VerdictKind = "better"
	Worse        VerdictKind = "worse"
	WithinNoise  VerdictKind = "within_noise"
	Inconclusive VerdictKind = "inconclusive"
)

// InconclusiveReason says why a comparison reached no verdict.
type InconclusiveReason string

// The reasons. The zero value is for verdicts that are conclusive.
const (
	ReasonNone                InconclusiveReason = ""
	ReasonTooFewSamples       InconclusiveReason = "too_few_samples"
	ReasonConstantSamples     InconclusiveReason = "constant_samples"
	ReasonMetricMismatch      InconclusiveReason = "metric_mismatch"
	ReasonTaskVersionMismatch InconclusiveReason = "task_version_mismatch"
)

// Verdict is a pure function of two sample sets and a metric's direction: the same samples always
// give the same verdict.
type Verdict struct {
	Kind             VerdictKind
	Reason           InconclusiveReason
	CandidateSamples uint32
	BaseSamples      uint32
	CandidateMean    float64
	BaseMean         float64
	// PValue is the two-sided p-value, zero when no test ran.
	PValue float64
}

// Comparison is a verdict with what it compared.
type Comparison struct {
	Commit  Commit
	Against Commit
	Verdict Verdict
	// Environment is the one both sides were measured in.
	Environment Fingerprint
}
