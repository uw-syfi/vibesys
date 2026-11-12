package wire

import (
	"errors"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The mappings of the New run contracts: the facts a run records and the commands that change it.

var (
	runPhases = newEnum(map[core.RunPhase]pb.RunPhase{
		"":                 pb.RunPhase_RUN_PHASE_UNSPECIFIED,
		core.PhaseSetup:    pb.RunPhase_RUN_PHASE_SETUP,
		core.PhaseOptimize: pb.RunPhase_RUN_PHASE_OPTIMIZE,
	})
	taskDomains = newEnum(map[core.TaskDomain]cliv1.TaskDomain{
		"":                        cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED,
		core.DomainLLMServing:     cliv1.TaskDomain_TASK_DOMAIN_LLM_SERVING,
		core.DomainDataStructures: cliv1.TaskDomain_TASK_DOMAIN_DATA_STRUCTURES,
		core.DomainDatabases:      cliv1.TaskDomain_TASK_DOMAIN_DATABASES,
		core.DomainMicroservices:  cliv1.TaskDomain_TASK_DOMAIN_MICROSERVICES,
	})
	modelMakers = newEnum(map[core.ModelMaker]cliv1.ModelMaker{
		"":                 cliv1.ModelMaker_MODEL_MAKER_UNSPECIFIED,
		core.MakerMeta:     cliv1.ModelMaker_MODEL_MAKER_META,
		core.MakerQwen:     cliv1.ModelMaker_MODEL_MAKER_QWEN,
		core.MakerDeepSeek: cliv1.ModelMaker_MODEL_MAKER_DEEPSEEK,
		core.MakerZhipu:    cliv1.ModelMaker_MODEL_MAKER_ZHIPU,
		core.MakerMoonshot: cliv1.ModelMaker_MODEL_MAKER_MOONSHOT,
		core.MakerAI2:      cliv1.ModelMaker_MODEL_MAKER_AI2,
		core.MakerOpenAI:   cliv1.ModelMaker_MODEL_MAKER_OPENAI,
		core.MakerOther:    cliv1.ModelMaker_MODEL_MAKER_OTHER,
	})
	vendors = newEnum(map[core.AcceleratorVendor]cliv1.AcceleratorVendor{
		"":                cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_UNSPECIFIED,
		core.VendorNvidia: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
		core.VendorAMD:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		core.VendorAWS:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AWS,
		core.VendorApple:  cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE,
		core.VendorCPU:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU,
	})
	accelerators = newEnum(map[core.AcceleratorModel]cliv1.AcceleratorModel{
		"":                     cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED,
		core.ModelMI210:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210,
		core.ModelMI250:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI250,
		core.ModelMI300A:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A,
		core.ModelMI300X:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X,
		core.ModelMI325X:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X,
		core.ModelH100:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100,
		core.ModelB200:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200,
		core.ModelL4:           cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4,
		core.ModelA100:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100,
		core.ModelTrainium2:    cliv1.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2,
		core.ModelAppleSilicon: cliv1.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON,
		core.ModelOther:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER,
	})
	cardDirections = newEnum(map[core.Direction]cliv1.TaskObjectiveDirection{
		"":            cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_UNSPECIFIED,
		core.Maximize: cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MAX,
		core.Minimize: cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN,
	})
	setupChecks = newEnum(map[core.SetupCheckID]pb.SetupCheckId{
		core.CheckObjective:        pb.SetupCheckId_SETUP_CHECK_ID_OBJECTIVE,
		core.CheckCorrectnessCheck: pb.SetupCheckId_SETUP_CHECK_ID_CORRECTNESS_CHECK,
		core.CheckBenchmark:        pb.SetupCheckId_SETUP_CHECK_ID_BENCHMARK,
		core.CheckBasePasses:       pb.SetupCheckId_SETUP_CHECK_ID_BASE_PASSES,
		core.CheckBrokenFails:      pb.SetupCheckId_SETUP_CHECK_ID_BROKEN_FAILS,
		core.CheckNumberStable:     pb.SetupCheckId_SETUP_CHECK_ID_NUMBER_STABLE,
	})
	leadChangeStates = newEnum(map[core.LeadChangeState]pb.LeadChangeState{
		core.LeadChangePending: pb.LeadChangeState_LEAD_CHANGE_STATE_PENDING,
		core.LeadChangeApplied: pb.LeadChangeState_LEAD_CHANGE_STATE_APPLIED,
	})
)

func needOut(n *core.AcceleratorNeed) *cliv1.AcceleratorNeed {
	if n == nil {
		return nil
	}
	return &cliv1.AcceleratorNeed{Vendor: vendors.out(n.Vendor), Model: accelerators.out(n.Model), Count: n.Count}
}

func needIn(n *cliv1.AcceleratorNeed, errs *errorList) *core.AcceleratorNeed {
	if n == nil {
		return nil
	}
	return &core.AcceleratorNeed{Vendor: vendors.in(n.GetVendor(), errs), Model: accelerators.in(n.GetModel(), errs), Count: n.GetCount()}
}

func cardOut(c *core.TaskCard) *cliv1.TaskCard {
	if c == nil {
		return nil
	}
	out := &cliv1.TaskCard{
		Task: c.Task, Domain: taskDomains.out(c.Domain), Hardware: needOut(c.Hardware), HardwareName: c.HardwareName,
		ModelName: c.ModelName, ModelMaker: modelMakers.out(c.ModelMaker), WorkloadName: c.WorkloadName,
		WorkloadDescription: c.WorkloadDescription, Heading: c.Heading,
	}
	for _, o := range c.Objectives {
		out.Objectives = append(out.Objectives, &cliv1.CardObjective{Name: o.Name, Direction: cardDirections.out(o.Direction), Unit: o.Unit})
	}
	return out
}

// TaskCardMessage is the card a client shows of a task; nil for none.
func TaskCardMessage(c *core.TaskCard) *cliv1.TaskCard { return cardOut(c) }

func cardIn(c *cliv1.TaskCard, errs *errorList) *core.TaskCard {
	if c == nil {
		return nil
	}
	out := &core.TaskCard{
		Task: c.GetTask(), Domain: taskDomains.in(c.GetDomain(), errs), Hardware: needIn(c.GetHardware(), errs),
		HardwareName: c.GetHardwareName(), ModelName: c.GetModelName(), ModelMaker: modelMakers.in(c.GetModelMaker(), errs),
		WorkloadName: c.GetWorkloadName(), WorkloadDescription: c.GetWorkloadDescription(), Heading: c.GetHeading(),
	}
	for _, o := range c.GetObjectives() {
		out.Objectives = append(out.Objectives, core.CardObjective{Name: o.GetName(), Direction: cardDirections.in(o.GetDirection(), errs), Unit: o.GetUnit()})
	}
	return out
}

func optionalUsageOut(u *core.Usage) *pb.Usage {
	if u == nil {
		return nil
	}
	if out := usageOut(*u); out != nil {
		return out
	}
	return &pb.Usage{}
}

func optionalUsageIn(u *pb.Usage) *core.Usage {
	if u == nil {
		return nil
	}
	out := usageIn(u)
	return &out
}

func specOut(s core.AgentSpec) *pb.AgentSpec {
	if s == (core.AgentSpec{}) {
		return nil
	}
	return &pb.AgentSpec{Harness: harnesses.out(s.Harness), Model: string(s.Model), ReasoningEffort: string(s.Effort)}
}

func specPointerIn(s *pb.AgentSpec, errs *errorList) *core.AgentSpec {
	if s == nil {
		return nil
	}
	out := specIn(s, errs)
	return &out
}

func checksIn(in []pb.SetupCheckId, errs *errorList) []core.SetupCheckID {
	var out []core.SetupCheckID
	for _, id := range in {
		out = append(out, setupChecks.in(id, errs))
	}
	return out
}

func checksOut(in []core.SetupCheckID) []pb.SetupCheckId {
	var out []pb.SetupCheckId
	for _, id := range in {
		out = append(out, setupChecks.out(id))
	}
	return out
}

func costLimitUpdateIn(u *pb.CostLimitUpdate) *core.LimitChange[float64] {
	if u == nil {
		return nil
	}
	return &core.LimitChange[float64]{Remove: u.GetRemove(), Set: u.GetSetUsd()}
}

func timeLimitUpdateIn(u *pb.TimeLimitUpdate) *core.LimitChange[time.Duration] {
	if u == nil {
		return nil
	}
	return &core.LimitChange[time.Duration]{Remove: u.GetRemove(), Set: u.GetSet().AsDuration()}
}

func optionalCost(usd float64) *float64 { return limitOut(usd) }

func optionalSpan(d time.Duration) *durationpb.Duration { return spanOut(d) }

func settingOut(c core.SettingChange) *pb.SettingChange {
	out := &pb.SettingChange{}
	switch {
	case c.CostLimit != nil:
		out.Change = &pb.SettingChange_CostLimit{CostLimit: &pb.CostLimitChange{From: optionalCost(c.CostLimit.From), To: optionalCost(c.CostLimit.To)}}
	case c.TimeLimit != nil:
		out.Change = &pb.SettingChange_TimeLimit{TimeLimit: &pb.TimeLimitChange{From: optionalSpan(c.TimeLimit.From), To: optionalSpan(c.TimeLimit.To)}}
	case c.Fanout != nil:
		out.Change = &pb.SettingChange_Fanout{Fanout: &pb.FanoutChange{From: c.Fanout.From, To: c.Fanout.To}}
	case c.Lead != nil:
		out.Change = &pb.SettingChange_Lead{Lead: &pb.LeadChange{From: specOut(c.Lead.From), To: specOut(c.Lead.To), State: leadChangeStates.out(c.Lead.State)}}
	}
	return out
}

func settingIn(c *pb.SettingChange, errs *errorList) core.SettingChange {
	switch b := c.GetChange().(type) {
	case *pb.SettingChange_CostLimit:
		return core.SettingChange{CostLimit: &core.CostLimitChange{From: b.CostLimit.GetFrom(), To: b.CostLimit.GetTo()}}
	case *pb.SettingChange_TimeLimit:
		return core.SettingChange{TimeLimit: &core.TimeLimitChange{From: b.TimeLimit.GetFrom().AsDuration(), To: b.TimeLimit.GetTo().AsDuration()}}
	case *pb.SettingChange_Fanout:
		return core.SettingChange{Fanout: &core.FanoutChange{From: b.Fanout.GetFrom(), To: b.Fanout.GetTo()}}
	case *pb.SettingChange_Lead:
		l := b.Lead
		return core.SettingChange{Lead: &core.LeadChange{
			From: specIn(l.GetFrom(), errs), To: specIn(l.GetTo(), errs), State: leadChangeStates.in(l.GetState(), errs),
		}}
	}
	errs.addf("setting change: unexpected body %T", c.GetChange())
	return core.SettingChange{}
}

func settingsUpdatedOut(u core.RunSettingsUpdated) *pb.RunUpdated {
	out := &pb.RunUpdated{RunId: string(u.Run)}
	for _, c := range u.Changes {
		out.Changes = append(out.Changes, settingOut(c))
	}
	return out
}

func settingsUpdatedIn(u *pb.RunUpdated, errs *errorList) core.RunSettingsUpdated {
	out := core.RunSettingsUpdated{Run: core.RunID(u.GetRunId())}
	for _, c := range u.GetChanges() {
		out.Changes = append(out.Changes, settingIn(c, errs))
	}
	return out
}

// commandIn reads the commands of the New run contracts; ok is false for any other body.
func commandIn(m *pb.Command, errs *errorList) (core.CommandBody, bool) {
	switch b := m.GetBody().(type) {
	case *pb.Command_UpdateRun:
		u := b.UpdateRun
		var fanout *uint32
		if u.FanoutLimit != nil {
			fanout = u.FanoutLimit
		}
		return core.UpdateRun{
			Run: core.RunID(u.GetRunId()), CostLimit: costLimitUpdateIn(u.GetCostLimit()),
			TimeLimit: timeLimitUpdateIn(u.GetTimeLimit()), Fanout: fanout, Lead: specPointerIn(u.GetLead(), errs),
		}, true
	case *pb.Command_BeginSetup:
		s := b.BeginSetup
		return core.BeginSetup{
			Repository: s.GetRepository(), Lead: specIn(s.GetLead(), errs), Brief: s.GetBrief(), Allow: choicesIn(s.GetAllowAgents(), errs),
		}, true
	case *pb.Command_CompleteSetup:
		s := b.CompleteSetup
		return core.CompleteSetup{
			Run: core.RunID(s.GetRunId()), Task: s.GetTask(), MaxCostUSD: s.GetMaxCostUsd(), MaxTime: s.GetMaxTime().AsDuration(),
			FanoutLimit: s.GetFanoutLimit(), Lead: specIn(s.GetLead(), errs), Brief: s.GetBrief(), EvalPlace: s.GetEvalPlace(),
		}, true
	case *pb.Command_DeclareTask:
		return core.DeclareTask{Run: core.RunID(b.DeclareTask.GetRunId()), Task: b.DeclareTask.GetTask()}, true
	}
	return nil, false
}

func pendingLeadOut(s *core.AgentSpec) *pb.PendingLead {
	if s == nil {
		return nil
	}
	return &pb.PendingLead{Harness: harnesses.out(s.Harness), Model: string(s.Model), ReasoningEffort: string(s.Effort)}
}

// TaskCardFromMessage reads a card the CLI's daemon answered, as the core holds it. The message is
// held to its contract first, so a card with an unknown field or a value out of its bounds is an
// error that names it.
func TaskCardFromMessage(m *cliv1.TaskCard) (core.TaskCard, error) {
	if m == nil {
		return core.TaskCard{}, errors.New("the task card is missing")
	}
	if err := Validate(m); err != nil {
		return core.TaskCard{}, err
	}
	var errs errorList
	card := cardIn(m, &errs)
	return *card, errs.err()
}

// NeedMessage is a task's need as the contract states it, for the packages that judge it against
// what a place gives.
func NeedMessage(n *core.AcceleratorNeed) *cliv1.AcceleratorNeed { return needOut(n) }

func pendingLeadIn(s *pb.PendingLead, errs *errorList) *core.AgentSpec {
	if s == nil {
		return nil
	}
	return &core.AgentSpec{Harness: harnesses.in(s.GetHarness(), errs), Model: core.ModelName(s.GetModel()), Effort: core.EffortName(s.GetReasoningEffort())}
}
