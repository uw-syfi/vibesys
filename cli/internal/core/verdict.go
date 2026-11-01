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
	ReasonNone InconclusiveReason = ""
	// ReasonTooFewSamples is a side with fewer than MinSamplesWithoutFloor samples, for a task
	// that declares no noise floor.
	ReasonTooFewSamples InconclusiveReason = "too_few_samples"
	// ReasonConstantSamples is samples that show no noise to test against: both sides are
	// constant, or the candidate's mean is zero, which makes the ratio's noise zero.
	ReasonConstantSamples     InconclusiveReason = "constant_samples"
	ReasonMetricMismatch      InconclusiveReason = "metric_mismatch"
	ReasonTaskVersionMismatch InconclusiveReason = "task_version_mismatch"
)

// Verdict is a pure function of two sample sets, a metric's direction and the task's declared
// noise floor: the same samples always give the same verdict. Compare in this package decides it.
type Verdict struct {
	Kind             VerdictKind
	Reason           InconclusiveReason
	CandidateSamples uint32
	BaseSamples      uint32
	CandidateMean    float64
	BaseMean         float64
	// Ratio is CandidateMean / BaseMean, raw: not oriented by the metric's direction and never
	// rounded. Rounding is for text only.
	Ratio float64
	// Noise is one standard error of Ratio. The verdict weighs the gain against a multiple of it.
	Noise float64
}

// Comparison is a verdict with what it compared.
type Comparison struct {
	Commit  Commit
	Against Commit
	Verdict Verdict
	// Environment is the one both sides were measured in.
	Environment Fingerprint
}
