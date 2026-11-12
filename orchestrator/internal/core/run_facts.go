package core

import "time"

// This file holds the data of the New run contracts: the facts a run records when it starts or leaves
// setup, and the commands that change a live run or move it out of setup. The behavior that decides
// them is not here: until it exists, the daemon refuses these commands before they reach Decide.

// RunPhase is which part of its life a run is in. The empty value is a run recorded before phases
// existed, which is optimizing.
type RunPhase string

// The phases.
const (
	PhaseSetup    RunPhase = "setup"
	PhaseOptimize RunPhase = "optimize"
)

// Optimizing says the run is in its optimize phase, or recorded none.
func (p RunPhase) Optimizing() bool { return p != PhaseSetup }

// TaskDomain is what an example task optimizes. The empty value is a task with no [catalogue].
type TaskDomain string

// The domains.
const (
	DomainLLMServing     TaskDomain = "llm_serving"
	DomainDataStructures TaskDomain = "data_structures"
	DomainDatabases      TaskDomain = "databases"
	DomainMicroservices  TaskDomain = "microservices"
)

// ModelMaker is who made a served model. The empty value is none.
type ModelMaker string

// The makers.
const (
	MakerMeta     ModelMaker = "meta"
	MakerQwen     ModelMaker = "qwen"
	MakerDeepSeek ModelMaker = "deepseek"
	MakerZhipu    ModelMaker = "zhipu"
	MakerMoonshot ModelMaker = "moonshot"
	MakerAI2      ModelMaker = "ai2"
	MakerOpenAI   ModelMaker = "openai"
	MakerOther    ModelMaker = "other"
)

// AcceleratorVendor is who makes an accelerator. The empty value is none.
type AcceleratorVendor string

// The vendors.
const (
	VendorNvidia AcceleratorVendor = "nvidia"
	VendorAMD    AcceleratorVendor = "amd"
	VendorAWS    AcceleratorVendor = "aws"
	VendorApple  AcceleratorVendor = "apple"
	VendorCPU    AcceleratorVendor = "cpu"
)

// AcceleratorModel is which device of a vendor. The empty value is any model of the vendor.
type AcceleratorModel string

// The models.
const (
	ModelMI210        AcceleratorModel = "mi210"
	ModelMI250        AcceleratorModel = "mi250"
	ModelMI300A       AcceleratorModel = "mi300a"
	ModelMI300X       AcceleratorModel = "mi300x"
	ModelMI325X       AcceleratorModel = "mi325x"
	ModelH100         AcceleratorModel = "h100"
	ModelB200         AcceleratorModel = "b200"
	ModelL4           AcceleratorModel = "l4"
	ModelA100         AcceleratorModel = "a100"
	ModelTrainium2    AcceleratorModel = "trainium2"
	ModelAppleSilicon AcceleratorModel = "apple_silicon"
	ModelOther        AcceleratorModel = "other"
)

// AcceleratorNeed is what a task wants of the place that evaluates it.
type AcceleratorNeed struct {
	Vendor AcceleratorVendor
	Model  AcceleratorModel
	Count  uint32
}

// CardObjective is one objective of a task, as a client lists it (Direction is Maximize or Minimize).
type CardObjective struct {
	Name      string
	Direction Direction
	Unit      string
}

// TaskCard is what a client shows of a task, as the host read it when the run started. The core
// records it and reads none of it.
type TaskCard struct {
	Task                string
	Domain              TaskDomain
	Hardware            *AcceleratorNeed
	HardwareName        string
	ModelName           string
	ModelMaker          ModelMaker
	WorkloadName        string
	WorkloadDescription string
	Objectives          []CardObjective
	Heading             string
}

// SetupCheckID names one of the checks a task passes before a run can optimize it.
type SetupCheckID string

// The checks.
const (
	CheckObjective        SetupCheckID = "objective"
	CheckCorrectnessCheck SetupCheckID = "correctness_check"
	CheckBenchmark        SetupCheckID = "benchmark"
	CheckBasePasses       SetupCheckID = "base_passes"
	CheckBrokenFails      SetupCheckID = "broken_fails"
	CheckNumberStable     SetupCheckID = "number_stable"
)

// LimitChange says what an update does to a limit that may be absent: set it, or remove it.
type LimitChange[T any] struct {
	Remove bool
	Set    T
}

// The commands that change a run or move it through setup.
type (
	// UpdateRun changes settings of a live run, all of them or none. A nil setting is left as it is.
	UpdateRun struct {
		Run RunID
		// CostLimit sets the cost limit in dollars, or removes it.
		CostLimit *LimitChange[float64]
		// TimeLimit sets the total time the run may take, or removes the limit.
		TimeLimit *LimitChange[time.Duration]
		// Fanout is the new fanout limit, at least one.
		Fanout *uint32
		// Lead is the spec the lead continues on.
		Lead *AgentSpec
	}
	// BeginSetup starts a run in its setup phase.
	BeginSetup struct {
		Repository string
		Lead       AgentSpec
		Brief      string
		Allow      []AgentChoice
		// JobTool is what the run's agents use, set by the shell as a start's is: no part of what the
		// client sent or of the command's digest.
		JobTool JobToolEndpoint
	}
	// CompleteSetup ends a run's setup phase and starts it optimizing the named task.
	CompleteSetup struct {
		Run         RunID
		Task        string
		MaxCostUSD  float64
		MaxTime     time.Duration
		FanoutLimit uint32
		Lead        AgentSpec
		Brief       string
		EvalPlace   string
		// Verified is what the shell established, afresh, before the command was decided: whether the
		// task's checks hold, and its card and eval place. Like a start's facts it is host attested and
		// no part of what the client sent or of the command's digest, so a retry gets its first receipt.
		Verified SetupVerdict
	}
	// DeclareTask names the task the setup lead is making. It sets Run.SetupTask.
	DeclareTask struct {
		Run  RunID
		Task string
	}
)

// SetupVerdict is the shell's reading of a task at the moment its setup is completed. The zero value
// is a command no shell verified, which completes nothing: it names no task, so none of its checks hold.
type SetupVerdict struct {
	// Task is the task the verdict is about, which must be the task completed.
	Task string
	// TaskVersion is the working tree's version of the task and BaseCommit the commit the run's result
	// branch will start from. The core records neither: they say what the verdict was read at.
	TaskVersion, BaseCommit string
	// Unmet are the checks that do not hold, empty when all do.
	Unmet []SetupCheckID
	// Facts are the task's card as committed at BaseCommit and the eval place, resolved as a start's are
	// (ResolveStartFacts), or the refusal of the place.
	Facts StartFacts
}

// SetupChecks lists the six checks in order.
var SetupChecks = []SetupCheckID{
	CheckObjective, CheckCorrectnessCheck, CheckBenchmark, CheckBasePasses, CheckBrokenFails, CheckNumberStable,
}

func (UpdateRun) action() Action     { return ActionUpdateRun }
func (BeginSetup) action() Action    { return ActionBeginSetup }
func (CompleteSetup) action() Action { return ActionCompleteSetup }
func (DeclareTask) action() Action   { return ActionDeclareTask }

// LeadChangeState says whether a change of the lead has taken effect.
type LeadChangeState string

// The states of a change of the lead.
const (
	LeadChangePending LeadChangeState = "pending"
	LeadChangeApplied LeadChangeState = "applied"
)

// SettingChange is one setting an update changed. Exactly one field is set.
type SettingChange struct {
	CostLimit *CostLimitChange
	TimeLimit *TimeLimitChange
	Fanout    *FanoutChange
	Lead      *LeadChange
}

// CostLimitChange is a change of the cost limit; zero is no limit.
type CostLimitChange struct{ From, To float64 }

// TimeLimitChange is a change of the total time allowed; zero is no limit.
type TimeLimitChange struct{ From, To time.Duration }

// FanoutChange is a change of the fanout limit.
type FanoutChange struct{ From, To uint32 }

// LeadChange is a change of the lead's spec.
type LeadChange struct {
	From, To AgentSpec
	State    LeadChangeState
}

// RunSettingsUpdated is the event of a committed change of a run's settings: every setting one update
// changed. It is information for the timeline: the run's own event carries the new values.
type RunSettingsUpdated struct {
	Run     RunID
	Changes []SettingChange
}

func (RunSettingsUpdated) eventBody() {}
