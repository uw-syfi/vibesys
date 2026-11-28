package core

// VerdictKind is the outcome of comparing a commit's samples with a base's.
type VerdictKind string

// The verdict kinds.
const (
	Better       VerdictKind = "better"
	Worse        VerdictKind = "worse"
	WithinNoise  VerdictKind = "within_noise"
	Inconclusive VerdictKind = "inconclusive"
	// Tradeoff is the overall verdict of a multi-objective comparison that is better on some
	// objective and worse on another. A single metric is never a tradeoff.
	Tradeoff VerdictKind = "tradeoff"
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

// VerdictBound is the bar that decided a conclusive verdict.
type VerdictBound string

// The bounds. The zero value is for an inconclusive verdict.
const (
	BoundNone VerdictBound = ""
	// BoundNoise is the noise test: the gain against a multiple of the noise.
	BoundNoise VerdictBound = "noise"
	// BoundMinGain is the objective's declared minimum gain. A Better verdict names it when it was
	// the higher bar, and a WithinNoise one when the gain passed the noise test but not it.
	BoundMinGain VerdictBound = "min_gain"
)

// SigmaSource is what set the relative variation of one run that a verdict's noise rests on.
type SigmaSource string

// The sources. The zero value is for a verdict that compared no numbers.
const (
	SigmaNone SigmaSource = ""
	// SigmaDeclaredFloor is the task's declared relative noise, which was at least the observed
	// spread.
	SigmaDeclaredFloor SigmaSource = "declared_floor"
	// SigmaObservedSpread is the spread the runs showed: above the declared relative noise, or the
	// task declares none.
	SigmaObservedSpread SigmaSource = "observed_spread"
)

// Verdict is a pure function of two sample sets, a metric's direction and the task's declared
// noise floor and minimum gain: the same samples always give the same verdict. Compare in this
// package decides it.
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
	// RelativeNoise is the noise floor the task declared and the comparison used, zero for none.
	RelativeNoise float64
	// MinGain is the smallest relative gain the task declared to count as better, zero for none.
	MinGain float64
	// DecidedBy is the bar that decided a conclusive verdict, BoundNone for an inconclusive one.
	DecidedBy VerdictBound
	// Critical is how many noises a gain had to exceed, zero for an inconclusive verdict.
	Critical float64
	// Threshold is the relative gain the bar named by DecidedBy stands at: Critical × Noise for
	// BoundNoise, MinGain for BoundMinGain. Zero for an inconclusive verdict.
	Threshold float64
	// SigmaSource says whether the declared relative noise or the observed spread set the noise.
	SigmaSource SigmaSource
	// ObservedSpread is the pooled relative deviation of one run from its own side's mean.
	ObservedSpread float64
	// SamplesToDecide is, for a WithinNoise verdict with a positive gain, the fewest samples a side
	// at which the same means and sigma would read Better; zero when no count would, and for any
	// other verdict.
	SamplesToDecide uint32
}

// Comparison is a verdict with what it compared.
type Comparison struct {
	Commit  Commit
	Against Commit
	// Verdict is the one verdict of a single-metric task. For a multi-objective (Pareto) task it
	// is the overall verdict of Objectives (see CombineObjectives): its kind, its reason and the
	// sample counts are set, and its means, ratio and noise are zero, since no one number stands
	// for every objective.
	Verdict Verdict
	// Objectives is the verdict on each objective of a multi-objective task, in the order the task
	// declares them. It is empty for a single-metric task.
	Objectives []ObjectiveVerdict
	// Environment is the one both sides were measured in.
	Environment Fingerprint
}
