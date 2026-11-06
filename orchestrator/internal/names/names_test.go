package names_test

import (
	"regexp"
	"testing"

	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

var word = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// roundTrips holds, for one enum, that each value but the unspecified one is spelled as a lower
// case word, that the spellings differ, and that a spelling reads back to its value.
func roundTrips[E names.Enum](t *testing.T) {
	var zero E
	d := zero.Descriptor()
	t.Run(string(d.Name()), func(t *testing.T) {
		seen := map[string]bool{}
		for i := 1; i < d.Values().Len(); i++ {
			v := E(d.Values().Get(i).Number())
			s := names.Spell(v)
			if !word.MatchString(s) {
				t.Errorf("%s is spelled %q, which is not a lower case word", d.Values().Get(i).Name(), s)
			}
			if seen[s] {
				t.Errorf("%q spells two values of %s", s, d.Name())
			}
			seen[s] = true
			if back, ok := names.Parse[E](s); !ok || back != v {
				t.Errorf("%q reads back as %v (%v), want %v", s, back, ok, v)
			}
		}
		if _, ok := names.Parse[E](names.Spell(zero)); ok && names.Spell(zero) != "0" {
			t.Errorf("the unspecified value is spelled %q and reads back", names.Spell(zero))
		}
		if len(seen) != len(names.Spellings(d)) {
			t.Errorf("Spellings lists %d values, the enum has %d", len(names.Spellings(d)), len(seen))
		}
	})
}

func TestEverySpellingOfEveryEnumTheOrchestratorSpeaksReadsBack(t *testing.T) {
	roundTrips[pb.Harness](t)
	roundTrips[pb.AgentRole](t)
	roundTrips[pb.AgentStatus](t)
	roundTrips[pb.Delivery](t)
	roundTrips[pb.ReportKind](t)
	roundTrips[pb.JudgeVerdict](t)
	roundTrips[pb.TurnStatus](t)
}

func TestTheSpellingsTheAgentsAndTheCommandLineKnow(t *testing.T) {
	cases := map[string]string{
		names.Spell(pb.Harness_HARNESS_CLAUDE_CODE):                "claude_code",
		names.Spell(pb.Harness_HARNESS_CODEX):                      "codex",
		names.Spell(pb.AgentRole_AGENT_ROLE_WORKER):                "worker",
		names.Spell(pb.AgentStatus_AGENT_STATUS_IDLE):              "idle",
		names.Spell(pb.Delivery_DELIVERY_NEW_TURN):                 "new_turn",
		names.Spell(pb.JudgeVerdict_JUDGE_VERDICT_PASS):            "pass",
		names.Spell(pb.AgentRole(99)):                              "99",
		names.List[pb.Harness]():                                   "claude_code or codex",
		names.List(pb.AgentRole_AGENT_ROLE_LEAD):                   "planner, worker, judge, combiner or profiler",
		names.Join([]string{"only"}):                               "only",
		names.Join(nil):                                            "",
		names.Join(names.Spellings(pb.ReportKind(0).Descriptor())): "progress or final",
	}
	for got, want := range cases {
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	}
	if _, ok := names.Lookup(pb.Harness(0).Descriptor(), "unspecified"); ok {
		t.Error("the unspecified value reads back")
	}
	var _ protoreflect.EnumDescriptor = pb.Harness(0).Descriptor()
}
