package wire_test

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

func commit(c byte) string { return strings.Repeat(string(c), 40) }

func sampleSetupFacts() *cliv1.SetupFacts {
	noise := 0.02
	exit := int32(3)
	at := timestamppb.New(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	return &cliv1.SetupFacts{
		Task: "kv", TaskVersion: "v2",
		Card: &cliv1.TaskCard{Task: "kv", Heading: "Fast KV", Objectives: []*cliv1.CardObjective{
			{Name: "qps", Direction: cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MAX, Unit: "ops/s"}}},
		Declarations: &cliv1.SetupDeclarations{
			Objectives:    []*cliv1.SetupObjective{{Name: "qps", Direction: cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MAX, Unit: "ops/s", NoiseFloor: &noise}},
			CheckDeclared: true, BenchmarkDeclared: true,
		},
		Base: &cliv1.SetupBase{Commit: commit('a'), TaskVersion: "v2"},
		Jobs: []*cliv1.SetupJob{{
			JobId: "j1", Kind: cliv1.JobKind_JOB_KIND_CHECK, State: cliv1.JobState_JOB_STATE_FAILED, Commit: commit('b'), TaskVersion: "v2",
			ExitCode: &exit, Relation: cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_DESCENDANT, CodeChanged: true,
			SubmittedAt: at, FinishedAt: at, FailedStage: cliv1.JobStage_JOB_STAGE_CHECK,
		}},
		Measurements: []*cliv1.Measurement{{
			Commit: commit('a'), TaskVersion: "v2",
			Measure: &cliv1.MeasureResult{
				Metric: &cliv1.Metric{Name: "qps", Unit: "ops/s"}, Value: 10, Samples: []float64{9, 10, 11},
				Observations: []*cliv1.Observation{{Name: "p99", Values: []float64{1, 2}}},
			},
		}},
	}
}

// A message maps to the facts it says, failed stage included.
func TestSetupFactsMapToTheFactsTheChecksReadIncludingTheFailedStage(t *testing.T) {
	got, err := wire.SetupFactsIn(sampleSetupFacts())
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	noise, exit := 0.02, int32(3)
	want := setup.Facts{
		Task: "kv", Version: "v2",
		Card: core.TaskCard{Task: "kv", Heading: "Fast KV", Objectives: []core.CardObjective{{Name: "qps", Direction: core.Maximize, Unit: "ops/s"}}},
		Decl: setup.Declarations{Objectives: []setup.Objective{{Name: "qps", Unit: "ops/s", NoiseFloor: &noise}}, CheckDeclared: true, BenchmarkDeclared: true},
		Base: setup.Base{Commit: commit('a'), Version: "v2"},
		Jobs: []setup.Job{{
			ID: "j1", Kind: setup.KindCheck, State: setup.JobFailed, Commit: commit('b'), Version: "v2", ExitCode: &exit,
			FailedStage: setup.StageCheck, Relation: setup.RelationDescendant, CodeChanged: true, SubmittedAt: at, FinishedAt: at,
		}},
		Measurements: []setup.Measurement{{
			Commit: commit('a'), Version: "v2", Metric: "qps", Unit: "ops/s", Value: 10, Samples: []float64{9, 10, 11},
			Observations: []setup.Series{{Name: "p99", Values: []float64{1, 2}}},
		}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %+v\nwant %+v", got, want)
	}
}

// What the mapping does not carry, and why. Every other field of the message changes the facts when it
// changes, which the test below holds for the contract as it stands and for any field added to it.
var setupFactsNotCarried = map[string]string{
	"vibesys.cli.v1.SetupFacts.jobs_truncated":     "the checks read the jobs they are given, whether or not there are more",
	"vibesys.cli.v1.SetupObjective.direction":      "no check reads which way an objective improves",
	"vibesys.cli.v1.Measurement.environment":       "the fingerprint says where it ran, which no check reads",
	"vibesys.cli.v1.Measurement.job_ids":           "which jobs were pooled is not a fact the checks read",
	"vibesys.cli.v1.MeasureResult.check_job_id":    "the check a measurement relies on is read from the jobs",
	"vibesys.cli.v1.MeasureResult.objectives":      "the declarations carry the objectives",
	"vibesys.cli.v1.MeasureResult.sample_services": "which server a sample ran on is not read",
	"vibesys.cli.v1.Metric.direction":              "no check reads which way a metric improves",
	"vibesys.cli.v1.Observation.value":             "the checks read the values and take their own spread",
	"vibesys.cli.v1.Observation.unit":              "the unit of an auxiliary series is not read",
}

// Every field of SetupFacts, however deep, either changes what it maps to or is named above as not
// carried, so a field the CLI adds and the mapping forgets fails here. The fields are found from the
// descriptor, and each is changed in turn from a message that has every message field present.
func TestEverySetupFactsFieldIsMappedOrSaidNotToBe(t *testing.T) {
	base := sampleSetupFacts()
	populate(base.ProtoReflect())
	want, err := wire.SetupFactsIn(base)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	var visit func(m protoreflect.Message)
	check := func(fd protoreflect.FieldDescriptor, change func() func()) {
		restore := change()
		got, err := wire.SetupFactsIn(base)
		restore()
		if err == nil && reflect.DeepEqual(got, want) {
			t.Errorf("changing %s changed nothing the checks read", fd.FullName())
		}
	}
	visit = func(m protoreflect.Message) {
		fields := m.Descriptor().Fields()
		for i := range fields.Len() {
			fd := fields.Get(i)
			name := string(fd.FullName())
			if _, skip := setupFactsNotCarried[name]; skip {
				seen[name] = true
				continue
			}
			switch {
			case fd.IsList() && fd.Message() != nil:
				list := m.Mutable(fd).List()
				for j := range list.Len() {
					visit(list.Get(j).Message())
				}
				check(fd, func() func() {
					n := list.Len()
					list.Append(list.NewElement())
					return func() { list.Truncate(n) }
				})
			case fd.IsList():
				list := m.Mutable(fd).List()
				check(fd, func() func() {
					n := list.Len()
					list.Append(protoreflect.ValueOfFloat64(7))
					return func() { list.Truncate(n) }
				})
			case fd.Message() != nil:
				visit(m.Mutable(fd).Message())
			default:
				check(fd, func() func() {
					old, had := m.Get(fd), m.Has(fd)
					m.Set(fd, other(fd, old))
					return func() {
						if had {
							m.Set(fd, old)
						} else {
							m.Clear(fd)
						}
					}
				})
			}
		}
	}
	visit(base.ProtoReflect())
	for name := range setupFactsNotCarried {
		if !seen[name] {
			t.Errorf("%s is named as not carried and is not in the contract: drop it from the list", name)
		}
	}
}

// populate makes every message field of m present, so that the fields beneath it are reachable.
func populate(m protoreflect.Message) {
	fields := m.Descriptor().Fields()
	for i := range fields.Len() {
		fd := fields.Get(i)
		if _, skip := setupFactsNotCarried[string(fd.FullName())]; skip {
			continue
		}
		switch {
		case fd.IsMap():
			panic("the contract grew a map the mapping may carry: teach this test about " + string(fd.FullName()))
		case fd.IsList() && fd.Message() != nil:
			list := m.Mutable(fd).List()
			if list.Len() == 0 {
				list.Append(list.NewElement())
			}
			for j := range list.Len() {
				populate(list.Get(j).Message())
			}
		case fd.Message() != nil && !fd.IsList():
			populate(m.Mutable(fd).Message())
		}
	}
}

// other is a value of fd's kind that differs from old.
func other(fd protoreflect.FieldDescriptor, old protoreflect.Value) protoreflect.Value {
	switch fd.Kind() {
	case protoreflect.BoolKind:
		return protoreflect.ValueOfBool(!old.Bool())
	case protoreflect.StringKind:
		return protoreflect.ValueOfString(old.String() + "x")
	case protoreflect.EnumKind:
		n := fd.Enum().Values()
		return protoreflect.ValueOfEnum(fd.Enum().Values().Get((int(old.Enum()) + 1) % n.Len()).Number())
	case protoreflect.Int32Kind:
		return protoreflect.ValueOfInt32(int32(old.Int()) + 1)
	case protoreflect.Int64Kind:
		return protoreflect.ValueOfInt64(old.Int() + 1)
	case protoreflect.Uint32Kind:
		return protoreflect.ValueOfUint32(uint32(old.Uint()) + 1)
	case protoreflect.Uint64Kind:
		return protoreflect.ValueOfUint64(old.Uint() + 1)
	case protoreflect.DoubleKind:
		return protoreflect.ValueOfFloat64(old.Float() + 1)
	}
	panic("teach other about " + fd.Kind().String() + " for " + string(fd.FullName()))
}

// A value of an enum that has no counterpart in the facts is an error that names the problem, never a
// default: an unspecified relation, a job of a kind that is not a fact, and a message with no card.
func TestSetupFactsThatCannotBeMappedAreRefused(t *testing.T) {
	for name, edit := range map[string]func(*cliv1.SetupFacts){
		"relation": func(m *cliv1.SetupFacts) {
			m.Jobs[0].Relation = cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNSPECIFIED
		},
		"kind":  func(m *cliv1.SetupFacts) { m.Jobs[0].Kind = cliv1.JobKind_JOB_KIND_COMMAND },
		"state": func(m *cliv1.SetupFacts) { m.Jobs[0].State = cliv1.JobState_JOB_STATE_UNSPECIFIED },
		"card":  func(m *cliv1.SetupFacts) { m.Card = nil },
	} {
		m := proto.Clone(sampleSetupFacts()).(*cliv1.SetupFacts)
		edit(m)
		if _, err := wire.SetupFactsIn(m); err == nil {
			t.Errorf("%s: mapped", name)
		}
	}
}
