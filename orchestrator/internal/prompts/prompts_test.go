package prompts_test

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/prompts"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// The goldens in testdata are what the Python renderer this package replaced wrote for each
// message.json beside them, so they pin the words agents were sent before the move. Each case is a
// message as the contract states it (protojson) and the prompt it renders to.

func goldenCases(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join("testdata", "*.message.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no golden cases in testdata: %v", err)
	}
	return paths
}

func readCase(t *testing.T, path string) (*pb.Message, core.Message) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var m pb.Message
	if err := protojson.Unmarshal(data, &m); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	message, err := wire.MailboxFromMessage(&m)
	if err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	return &m, message
}

func TestEveryGoldenRendersToTheBytesThePythonRendererWrote(t *testing.T) {
	for _, path := range goldenCases(t) {
		name := strings.TrimSuffix(filepath.Base(path), ".message.json")
		t.Run(name, func(t *testing.T) {
			_, message := readCase(t, path)
			want, err := os.ReadFile(filepath.Join("testdata", name+".prompt"))
			if err != nil {
				t.Fatal(err)
			}
			got, err := prompts.Render(message)
			if err != nil {
				t.Fatal(err)
			}
			if got != string(want) {
				t.Fatalf("rendered:\n%q\nwant:\n%q", got, want)
			}
		})
	}
}

// The goldens must leave no kind of message, and no value of an enum a template words, unpinned.
func TestTheGoldensCoverEveryBodyAndEveryEnumValueTheWordsName(t *testing.T) {
	bodies := map[string]bool{}
	reportKinds := map[pb.ReportKind]bool{}
	outcomes := map[pb.JobOutcome]bool{}
	statuses := map[pb.AgentStatus]bool{}
	for _, path := range goldenCases(t) {
		m, _ := readCase(t, path)
		bodies[string(m.ProtoReflect().WhichOneof(m.ProtoReflect().Descriptor().Oneofs().ByName("body")).Name())] = true
		if r := m.GetReport(); r != nil {
			reportKinds[r.GetKind()] = true
		}
		if j := m.GetJobFinished(); j != nil {
			outcomes[j.GetOutcome()] = true
		}
		if s := m.GetSubagentSettled(); s != nil {
			statuses[s.GetStatus()] = true
		}
	}
	fields := (&pb.Message{}).ProtoReflect().Descriptor().Oneofs().ByName("body").Fields()
	for i := range fields.Len() {
		if name := string(fields.Get(i).Name()); !bodies[name] {
			t.Errorf("no golden for a message with body %s", name)
		}
	}
	covered := func(enum protoreflect.EnumDescriptor, has func(protoreflect.EnumNumber) bool) {
		for i := 1; i < enum.Values().Len(); i++ { // value 0 is unspecified.
			if v := enum.Values().Get(i); !has(v.Number()) {
				t.Errorf("no golden for %s", v.FullName())
			}
		}
	}
	covered(pb.ReportKind(0).Descriptor(), func(n protoreflect.EnumNumber) bool { return reportKinds[pb.ReportKind(n)] })
	covered(pb.JobOutcome(0).Descriptor(), func(n protoreflect.EnumNumber) bool { return outcomes[pb.JobOutcome(n)] })
	covered(pb.AgentStatus(0).Descriptor(), func(n protoreflect.EnumNumber) bool { return statuses[pb.AgentStatus(n)] })
}

// text draws a field value from the contract's strings: anything, so it may hold every character
// a template or a format verb gives meaning to, and it may be empty.
func text(r *rand.Rand, tag string, maxLen int) string {
	pieces := []string{
		"{{", "}}", "{%", "%}", "{#", "#}", "{{.Task}}", "{{ end }}", "%s", "%d", "%!v(MISSING)",
		"<no value>", "<nil>", "<b>&amp;</b>", `\n`, "\\", "$", "`", "'", `"`, " ", "  ", "\t", "\n", "\r\n",
		"a", "Z", "7", "é", "日本語", "😀", "\u00a0", "\u2003",
	}
	var b strings.Builder
	b.WriteString(tag)
	for range r.IntN(maxLen + 1) {
		b.WriteString(pieces[r.IntN(len(pieces))])
	}
	return b.String()
}

// optional is a value that is empty about a third of the time.
func optional(r *rand.Rand, tag string) string {
	if r.IntN(3) == 0 {
		return ""
	}
	return text(r, tag, 6)
}

// bodies draws one message of each body kind, with the fields the contract requires set to
// something, and the optional ones sometimes empty.
func bodies(r *rand.Rand) []core.Message {
	reportKinds := []core.ReportKind{core.ReportProgress, core.ReportFinal}
	outcomes := []core.JobState{core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost}
	statuses := []core.AgentStatus{
		core.AgentLaunching, core.AgentActive, core.AgentWaiting, core.AgentIdle, core.AgentEnded, core.AgentFailed, core.AgentLost,
	}
	envelope := func(body core.MessageBody) core.Message {
		return core.Message{
			ID: "m1", Run: "r1", From: core.AgentID(optional(r, "from:")), To: "agent-a",
			Delivery: core.DeliveryNewTurn, At: time.Unix(0, 0).UTC(), Body: body,
		}
	}
	return []core.Message{
		envelope(core.Text{Text: text(r, "text:", 8)}),
		envelope(core.Brief{Text: text(r, "brief:", 8)}),
		envelope(core.LeadBrief{Task: text(r, "task:", 4), FanoutLimit: r.Uint32(), Steering: optional(r, "steering:")}),
		envelope(core.Handoff{
			PlanNumber: r.Uint32(), HasCandidate: r.IntN(2) == 0, Claim: optional(r, "claim:"),
			Commit: core.Commit(optional(r, "commit:")), ParentCommit: core.Commit(optional(r, "parent:")),
			Branch: optional(r, "branch:"), Checks: optional(r, "checks:"), Measurement: optional(r, "measurement:"),
			Learned: optional(r, "learned:"), Next: optional(r, "next:"), OpenItems: optional(r, "open:"),
		}),
		envelope(core.Report{
			Kind: reportKinds[r.IntN(len(reportKinds))], Sequence: r.Uint32(), Headline: text(r, "headline:", 4),
			Changed: optional(r, "changed:"), InFlight: optional(r, "flight:"), Decisions: optional(r, "decisions:"),
			OpenForUser: optional(r, "user:"),
		}),
		envelope(core.JobFinished{
			Job: core.JobID(text(r, "job:", 3)), Outcome: outcomes[r.IntN(len(outcomes))],
			SubmittedBy: core.AgentID(optional(r, "by:")), Detail: optional(r, "detail:"),
		}),
		envelope(core.SubagentSettled{
			Agent: core.AgentID(text(r, "agent:", 3)), Status: statuses[r.IntN(len(statuses))], Detail: optional(r, "detail:"),
		}),
		envelope(core.HandoffReminder{}),
		envelope(core.LeadIdleReminder{Idle: time.Duration(r.Uint32()) * time.Second}),
	}
}

// fieldsOf lists the strings a message's body holds, which its prompt carries or leaves out.
func fieldsOf(m core.Message) []string {
	switch b := m.Body.(type) {
	case core.Text:
		return []string{b.Text}
	case core.Brief:
		return []string{b.Text}
	case core.LeadBrief:
		return []string{b.Task, b.Steering}
	case core.Handoff:
		fields := []string{string(m.From), b.Learned, b.Next, b.OpenItems}
		if b.HasCandidate { // a handoff with no candidate says nothing of one.
			fields = append(fields, string(b.Commit), string(b.ParentCommit), b.Branch, b.Claim, b.Checks, b.Measurement)
		}
		return fields
	case core.Report:
		return []string{string(m.From), b.Headline, b.Changed, b.InFlight, b.Decisions, b.OpenForUser}
	case core.JobFinished:
		return []string{string(b.Job), b.Detail}
	case core.SubagentSettled:
		return []string{string(b.Agent), b.Detail}
	}
	return nil
}

// artifacts are what a template engine or a format verb leaves behind when it goes wrong. No word
// the templates say contains one.
var artifacts = []string{"<no value>", "<nil>", "%!", "{{", "}}", "{%", "%}", "{#", "#}", "[]", "map["}

func TestEveryBodyRendersWordsWithNoTemplateArtifactsWhateverItsFieldsHold(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 9))
	for range 2000 {
		for _, m := range bodies(r) {
			got, err := prompts.Render(m)
			name := fmt.Sprintf("%T", m.Body)
			if err != nil {
				t.Fatalf("%s %+v: %v", name, m.Body, err)
			}
			if got == "" || got != strings.TrimSpace(got) {
				t.Fatalf("%s %+v: rendered %q, want words with no blanks around them", name, m.Body, got)
			}
			// Take the fields out; what is left is the words the templates say.
			rest := got
			for _, field := range fieldsOf(m) {
				if field = strings.TrimSpace(field); field != "" {
					rest = strings.ReplaceAll(rest, field, "\x00")
				}
			}
			for _, bad := range artifacts {
				if strings.Contains(rest, bad) {
					t.Fatalf("%s %+v: rendered %q, whose own words hold %q", name, m.Body, got, bad)
				}
			}
			for _, field := range fieldsOf(m) {
				if field = strings.TrimSpace(field); !strings.Contains(got, field) {
					t.Fatalf("%s %+v: rendered %q, which lost the field %q", name, m.Body, got, field)
				}
			}
		}
	}
}

// A template says an optional field's line exactly when the field is set. The values start with a
// tag, and none of the pieces they are drawn from spells a label, so a label in the words is the
// template's.
func TestAnOptionalFieldIsSaidExactlyWhenItIsSet(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	for range 2000 {
		for _, m := range bodies(r) {
			got, err := prompts.Render(m)
			if err != nil {
				t.Fatal(err)
			}
			said := func(label string) bool { return strings.Contains(got, label) }
			switch b := m.Body.(type) {
			case core.LeadBrief:
				if said("steering note") != (b.Steering != "") {
					t.Fatalf("steering %q, rendered %q", b.Steering, got)
				}
			case core.Handoff:
				if said("\nCandidate: commit ") != b.HasCandidate || said("\nIt produced no candidate.") == b.HasCandidate {
					t.Fatalf("has_candidate %v, rendered %q", b.HasCandidate, got)
				}
				if said("\nOpen items: ") != (b.OpenItems != "") {
					t.Fatalf("open items %q, rendered %q", b.OpenItems, got)
				}
			case core.Report:
				for label, value := range map[string]string{
					"\nChanged: ": b.Changed, "\nIn flight: ": b.InFlight, "\nDecisions: ": b.Decisions, "\nOpen for the user: ": b.OpenForUser,
				} {
					if said(label) != (value != "") {
						t.Fatalf("%q with value %q, rendered %q", label, value, got)
					}
				}
			case core.JobFinished:
				if b.Detail == "" && got != fmt.Sprintf("Job %s finished: %s.", b.Job, b.Outcome) {
					t.Fatalf("detail unset, rendered %q", got)
				}
				if b.Detail != "" && !strings.HasSuffix(got, ". "+strings.TrimSpace(b.Detail)) {
					t.Fatalf("detail %q, rendered %q", b.Detail, got)
				}
			case core.SubagentSettled:
				if b.Detail == "" && got != fmt.Sprintf("Agent %s stopped without a handoff (%s).", b.Agent, b.Status) {
					t.Fatalf("detail unset, rendered %q", got)
				}
			}
		}
	}
}

func TestRenderingIsPure(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 8))
	for range 500 {
		for _, m := range bodies(r) {
			before := m
			first, err := prompts.Render(m)
			if err != nil {
				t.Fatal(err)
			}
			second, err := prompts.Render(m)
			if err != nil || first != second {
				t.Fatalf("%+v rendered %q then %q (%v)", m.Body, first, second, err)
			}
			if !reflect.DeepEqual(before, m) {
				t.Fatalf("Render changed its argument: %+v became %+v", before, m)
			}
			// What a message says does not depend on where it is in its delivery.
			other := m
			other.ID, other.Run, other.To = "m2", "r2", "agent-b"
			other.Delivery, other.Rejection, other.Attempts = core.DeliverySteered, core.RejectAgentGone, 3
			other.At = time.Unix(99, 0).UTC()
			if again, err := prompts.Render(other); err != nil || again != first {
				t.Fatalf("%+v rendered %q with another envelope (%v), but %q with its own", m.Body, again, err, first)
			}
		}
	}
}

// The words of a message read back from the outbox are the words it had when it was built, which
// is what lets a retried delivery send what the first one did.
func TestAMessageRendersTheSameAfterTheRoundTripThroughItsStoredForm(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 12))
	for range 500 {
		for _, m := range bodies(r) {
			if idle, ok := m.Body.(core.LeadIdleReminder); ok {
				m.Body = core.LeadIdleReminder{Idle: idle.Idle + time.Duration(r.IntN(1000))*time.Millisecond}
			}
			want, err := prompts.Render(m)
			if err != nil {
				t.Fatal(err)
			}
			back, err := wire.MailboxFromMessage(wire.MailboxMessage(m))
			if err != nil {
				t.Fatal(err)
			}
			if got, err := prompts.Render(back); err != nil || got != want {
				t.Fatalf("%+v rendered %q, and %q after a round trip (%v)", m.Body, want, got, err)
			}
		}
	}
}

func TestTheIdleReminderNamesWholeMinutesOfIdleness(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 14))
	for range 2000 {
		idle := time.Duration(r.Int64N(int64(48*time.Hour))) + time.Duration(r.IntN(1000))*time.Microsecond
		got, err := prompts.Render(core.Message{Body: core.LeadIdleReminder{Idle: idle}})
		if err != nil {
			t.Fatal(err)
		}
		if want := fmt.Sprintf("for %d minutes:", int(idle/time.Minute)); !strings.Contains(got, want) {
			t.Fatalf("idle %v rendered %q, want it to say %q", idle, got, want)
		}
	}
}

func TestAMessageWithNoBodyHasNoPrompt(t *testing.T) {
	if got, err := prompts.Render(core.Message{ID: "no-body"}); !errors.Is(err, prompts.ErrNoPrompt) {
		t.Errorf("rendered %q (%v), want ErrNoPrompt", got, err)
	}
}

// Words of nothing but blanks are no words, but they are not an error: a turn the host cannot word
// would be offered again for ever and hold back every later command of its session.
func TestBlankWordsRenderToNothingAndFailNothing(t *testing.T) {
	for _, body := range []core.MessageBody{core.Text{Text: " \n\t\u00a0\x1f "}, core.Brief{}} {
		if got, err := prompts.Render(core.Message{ID: "blank", Body: body}); err != nil || got != "" {
			t.Errorf("%+v rendered %q (%v), want nothing and no error", body, got, err)
		}
	}
}
