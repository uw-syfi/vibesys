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

// The goldens in testdata pin the words agents are sent. Each case is a message as the contract
// states it (protojson) and the prompt it renders to. The lead idle reminder's were written for
// this package; the others were written by the Python renderer it replaced, so they also pin the
// words as they were before the move.

func goldenCases(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join("testdata", "*.message.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no golden cases in testdata: %v", err)
	}
	return paths
}

// records is the check-ins a catch-up request is worded from.
type records map[core.CheckInID]core.CheckIn

func (r records) CheckIn(id core.CheckInID) (core.CheckIn, bool) {
	c, ok := r[id]
	return c, ok
}

// readRecords reads the check-in a golden case's request is for, from <name>.checkin.json, when it has one.
func readRecords(t *testing.T, name string) records {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", name+".checkin.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	var c pb.CheckIn
	if err := protojson.Unmarshal(data, &c); err != nil {
		t.Fatalf("%s check-in: %v", name, err)
	}
	check, err := wire.CheckInFromMessage(&c)
	if err != nil {
		t.Fatalf("%s check-in: %v", name, err)
	}
	return records{check.ID: check}
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
			got, err := prompts.RenderWith(message, readRecords(t, name))
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
	verdicts := map[pb.JudgeVerdict]bool{}
	handoffs := map[string]bool{}
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
		if h := m.GetHandoff(); h != nil {
			handoffs[string(h.ProtoReflect().WhichOneof(h.ProtoReflect().Descriptor().Oneofs().ByName("body")).Name())] = true
			if w := h.GetWorker(); w != nil {
				handoffs[string(w.ProtoReflect().WhichOneof(w.ProtoReflect().Descriptor().Oneofs().ByName("outcome")).Name())] = true
			}
			if j := h.GetJudge(); j != nil {
				verdicts[j.GetVerdict()] = true
			}
		}
	}
	for _, group := range []protoreflect.OneofDescriptor{
		(&pb.Handoff{}).ProtoReflect().Descriptor().Oneofs().ByName("body"), (&pb.WorkerHandoff{}).ProtoReflect().Descriptor().Oneofs().ByName("outcome"),
	} {
		for i := range group.Fields().Len() {
			if name := string(group.Fields().Get(i).Name()); !handoffs[name] {
				t.Errorf("no golden for a handoff with %s", name)
			}
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
	covered(pb.JudgeVerdict(0).Descriptor(), func(n protoreflect.EnumNumber) bool { return verdicts[pb.JudgeVerdict(n)] })
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
func bodies(r *rand.Rand, rec records) []core.Message {
	reportKinds := []core.ReportKind{core.ReportProgress, core.ReportFinal}
	outcomes := []core.JobState{core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost}
	statuses := []core.AgentStatus{
		core.AgentLaunching, core.AgentActive, core.AgentWaiting, core.AgentIdle, core.AgentEnded, core.AgentFailed, core.AgentLost,
	}
	verdicts := []core.Verdict{core.VerdictPass, core.VerdictFail, core.VerdictUnmeasured}
	since := time.Date(2026, time.January, 1, 9, 0, 0, 0, time.UTC).Add(time.Duration(r.Uint32()) * time.Second)
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
		envelope(handover(r, reportKinds, statuses)),
		envelope(handoff(r, core.WorkerCandidate{
			Commit: core.Commit(optional(r, "commit:")), ParentCommit: core.Commit(optional(r, "parent:")),
			Branch: optional(r, "branch:"), Checks: optional(r, "checks:"), Measurement: optional(r, "measurement:"),
		})),
		envelope(handoff(r, core.WorkerNoCandidate{Blocker: optional(r, "blocker:")})),
		envelope(handoff(r, core.PlannerPlan{
			Plan: optional(r, "planfile:"), Confidence: optional(r, "confidence:"), Unestablished: optional(r, "unestablished:"),
			DecidingMeasurement: optional(r, "deciding:"),
		})),
		envelope(handoff(r, core.JudgeFinding{
			Commit: core.Commit(optional(r, "commit:")), Verdict: verdicts[r.IntN(len(verdicts))], Reasons: optional(r, "reasons:"),
		})),
		envelope(handoff(r, core.CombinerResult{
			Commit: core.Commit(optional(r, "commit:")), Branch: optional(r, "branch:"), Applied: optional(r, "applied:"),
			Unapplied: optional(r, "notdone:"), Checks: optional(r, "checks:"), Measurement: optional(r, "measurement:"),
		})),
		envelope(handoff(r, core.ProfilerFindings{
			Question: optional(r, "question:"), Revision: core.Commit(optional(r, "revision:")), Answer: optional(r, "answer:"),
			Evidence: optional(r, "evidence:"), Unsupported: optional(r, "unsupported:"), Informs: optional(r, "informs:"),
		})),
		envelope(core.Report{
			Kind: reportKinds[r.IntN(len(reportKinds))], Sequence: r.Uint32(), Headline: text(r, "headline:", 4),
			Changed: optional(r, "changed:"), InFlight: optional(r, "flight:"), Decisions: optional(r, "decisions:"),
		}),
		envelope(core.JobFinished{
			Job: core.JobID(text(r, "job:", 3)), Outcome: outcomes[r.IntN(len(outcomes))],
			SubmittedBy: core.AgentID(optional(r, "by:")), Detail: optional(r, "detail:"),
		}),
		envelope(core.SubagentSettled{
			Agent: core.AgentID(text(r, "agent:", 3)), Status: statuses[r.IntN(len(statuses))], Detail: optional(r, "detail:"),
		}),
		envelope(core.HandoffReminder{}),
		envelope(core.RunLimitReached{
			Limit: []core.RunLimit{core.LimitTime, core.LimitCost}[r.IntN(2)], Elapsed: time.Duration(r.Uint32()) * time.Second,
			Spent: spent(r), MaxCost: r.Float64() * 100, Grace: time.Duration(r.Uint32()) * time.Second,
		}),
		envelope(core.LeadIdleReminder{
			Idle: time.Duration(r.Uint32()) * time.Second, Subagents: r.Uint32(), Workers: r.Uint32(), Judges: r.Uint32(),
		}),
		envelope(core.ReportDue{Every: time.Duration(r.Uint32()) * time.Second, Reports: r.Uint32()}),
		envelope(catchUp(r, rec, since)),
	}
}

// handover draws what a replacement lead is told: every optional part set about half the time.
func handover(r *rand.Rand, reportKinds []core.ReportKind, statuses []core.AgentStatus) core.LeadHandover {
	roles := []core.AgentRole{core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler}
	h := core.LeadHandover{
		Task: text(r, "task:", 4), FanoutLimit: r.Uint32(),
		Previous: core.AgentSpec{Harness: []core.Harness{core.ClaudeCode, core.Codex}[r.IntN(2)], Model: core.ModelName(optional(r, "model:"))},
		Elapsed:  time.Duration(r.Uint32()) * time.Second, Spent: spent(r), ReportsPosted: r.Uint32(),
		AgentsEnded: r.Uint32(), ActiveJobs: r.Uint32(),
	}
	if r.IntN(2) == 0 {
		h.MaxCost = 1 + r.Float64()*100
	}
	if r.IntN(2) == 0 {
		h.MaxTime = time.Duration(1+r.Uint32N(1<<20)) * time.Second
	}
	if r.IntN(3) == 0 {
		h.LimitReached = []core.RunLimit{core.LimitTime, core.LimitCost}[r.IntN(2)]
	}
	if r.IntN(2) == 0 {
		h.LatestReport = &core.Report{
			Kind: reportKinds[r.IntN(len(reportKinds))], Scope: core.ReportScopeRun, Sequence: r.Uint32(), Headline: text(r, "headline:", 4),
			Changed: optional(r, "changed:"), InFlight: optional(r, "flight:"), Decisions: optional(r, "decisions:"),
		}
	}
	for i := range r.IntN(4) {
		h.Agents = append(h.Agents, core.HandoverAgent{
			ID: core.AgentID(text(r, fmt.Sprint("agent", i, ":"), 3)), Role: roles[r.IntN(len(roles))],
			Status: statuses[r.IntN(len(statuses))], PlanNumber: r.Uint32N(5),
		})
	}
	return h
}

// catchUp draws a request for a report and the ready check-in it is worded from, which it puts in rec.
func catchUp(r *rand.Rand, rec records, since time.Time) core.CatchUpRequest {
	until := since.Add(time.Duration(r.Uint32()) * time.Second)
	best := func(tag string) *core.Best {
		return &core.Best{Value: r.NormFloat64() * 1000, Job: core.JobID(text(r, tag+"job:", 3)), Commit: core.Commit(text(r, tag+"commit:", 3)), At: since}
	}
	facts := core.WindowFacts{
		Spend:         core.Spend{Turns: r.Uint32N(50)},
		AgentsStarted: r.Uint32N(9), AgentsEnded: r.Uint32N(9), JobsMeasured: r.Uint32N(99),
	}
	facts.Spend.Unpriced = r.Uint32N(facts.Spend.Turns + 1)
	if r.IntN(2) == 0 {
		facts.Spend.Usage.CostUSD = spent(r)
	}
	for i := range r.IntN(3) {
		move := core.ObjectiveMove{Metric: text(r, fmt.Sprint("metric", i, ":"), 3), Direction: []core.Direction{core.Maximize, core.Minimize}[r.IntN(2)], End: best(fmt.Sprint("end", i))}
		if r.IntN(2) == 0 {
			move.Start = best(fmt.Sprint("start", i))
		}
		facts.Objectives = append(facts.Objectives, move)
	}
	for i := range r.IntN(4) {
		facts.PlansCreated = append(facts.PlansCreated, uint32(1+i))
	}
	for i := range r.IntN(3) {
		facts.PlansChanged = append(facts.PlansChanged, uint32(10+i))
	}
	for i := range r.IntN(3) {
		facts.Reports = append(facts.Reports, core.MessageID(fmt.Sprint("report", i)))
	}
	rec["c1"] = core.CheckIn{ID: "c1", Run: "r1", Since: since, At: until, Outcome: core.CheckInReady{Facts: facts}}
	return core.CatchUpRequest{CheckIn: "c1", Since: since, Until: until}
}

// spent is a cost so far, absent about a third of the time.
func spent(r *rand.Rand) *float64 {
	if r.IntN(3) == 0 {
		return nil
	}
	usd := r.Float64() * 100
	return &usd
}

// handoff draws the fields every handoff has around the body of a role.
func handoff(r *rand.Rand, body core.HandoffBody) core.Handoff {
	return core.Handoff{
		PlanNumber: r.Uint32(), Claim: optional(r, "claim:"), Learned: optional(r, "learned:"), Next: optional(r, "next:"),
		OpenItems: optional(r, "open:"), Body: body,
	}
}

// fieldsOf lists the strings a message's body holds, which its prompt carries or leaves out.
func fieldsOf(m core.Message, rec records) []string {
	switch b := m.Body.(type) {
	case core.CatchUpRequest:
		var fields []string
		for _, o := range rec[b.CheckIn].Outcome.(core.CheckInReady).Facts.Objectives {
			fields = append(fields, o.Metric, string(o.End.Job), string(o.End.Commit))
			if o.Start != nil {
				fields = append(fields, string(o.Start.Job), string(o.Start.Commit))
			}
		}
		return fields
	case core.Text:
		return []string{b.Text}
	case core.Brief:
		return []string{b.Text}
	case core.LeadBrief:
		return []string{b.Task, b.Steering}
	case core.LeadHandover:
		fields := []string{b.Task, string(b.Previous.Model)}
		if b.LatestReport != nil {
			fields = append(fields, b.LatestReport.Headline, b.LatestReport.Changed, b.LatestReport.InFlight, b.LatestReport.Decisions)
		}
		for _, a := range b.Agents {
			fields = append(fields, string(a.ID))
		}
		return fields
	case core.Handoff:
		fields := []string{string(m.From), b.Claim, b.Learned, b.Next, b.OpenItems}
		switch k := b.Body.(type) {
		case core.WorkerCandidate:
			fields = append(fields, string(k.Commit), string(k.ParentCommit), k.Branch, k.Checks, k.Measurement)
		case core.WorkerNoCandidate:
			fields = append(fields, k.Blocker)
		case core.PlannerPlan:
			fields = append(fields, k.Plan, k.Confidence, k.Unestablished, k.DecidingMeasurement)
		case core.JudgeFinding:
			fields = append(fields, string(k.Commit), k.Reasons)
		case core.CombinerResult:
			fields = append(fields, string(k.Commit), k.Branch, k.Applied, k.Unapplied, k.Checks, k.Measurement)
		case core.ProfilerFindings:
			fields = append(fields, k.Question, string(k.Revision), k.Answer, k.Evidence, k.Unsupported, k.Informs)
		}
		return fields
	case core.Report:
		return []string{string(m.From), b.Headline, b.Changed, b.InFlight, b.Decisions}
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
		rec := records{}
		for _, m := range bodies(r, rec) {
			got, err := prompts.RenderWith(m, rec)
			name := fmt.Sprintf("%T", m.Body)
			if err != nil {
				t.Fatalf("%s %+v: %v", name, m.Body, err)
			}
			if got == "" || got != strings.TrimSpace(got) {
				t.Fatalf("%s %+v: rendered %q, want words with no blanks around them", name, m.Body, got)
			}
			// Take the fields out; what is left is the words the templates say.
			rest := got
			for _, field := range fieldsOf(m, rec) {
				if field = strings.TrimSpace(field); field != "" {
					rest = strings.ReplaceAll(rest, field, "\x00")
				}
			}
			for _, bad := range artifacts {
				if strings.Contains(rest, bad) {
					t.Fatalf("%s %+v: rendered %q, whose own words hold %q", name, m.Body, got, bad)
				}
			}
			for _, field := range fieldsOf(m, rec) {
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
		rec := records{}
		for _, m := range bodies(r, rec) {
			got, err := prompts.RenderWith(m, rec)
			if err != nil {
				t.Fatal(err)
			}
			said := func(label string) bool { return strings.Contains(got, label) }
			switch b := m.Body.(type) {
			case core.LeadBrief:
				if said("steering note") != (b.Steering != "") {
					t.Fatalf("steering %q, rendered %q", b.Steering, got)
				}
			case core.LeadHandover:
				if said("\nChanged: ") != (b.LatestReport != nil && b.LatestReport.Changed != "") ||
					said("\nIn flight: ") != (b.LatestReport != nil && b.LatestReport.InFlight != "") ||
					said("\nDecisions: ") != (b.LatestReport != nil && b.LatestReport.Decisions != "") ||
					said("\nHeadline: ") != (b.LatestReport != nil) || said("No report about the whole run") != (b.LatestReport == nil) {
					t.Fatalf("report %+v, rendered %q", b.LatestReport, got)
				}
				if said("\n  - ") != (len(b.Agents) > 0) || strings.Count(got, "\n  - ") != len(b.Agents) {
					t.Fatalf("%d agents, rendered %q", len(b.Agents), got)
				}
				if said("The run has already reached its") != (b.LimitReached != "") || said("Finish now") != (b.LimitReached != "") ||
					said("Read the run's records") != (b.LimitReached == "") {
					t.Fatalf("limit %q, rendered %q", b.LimitReached, got)
				}
				if said("no cost reported yet") != (b.Spent == nil) || said("it may cost") != (b.MaxCost != 0) || said("the run may take") != (b.MaxTime != 0) {
					t.Fatalf("spent %v, max cost %v, max time %v, rendered %q", b.Spent, b.MaxCost, b.MaxTime, got)
				}
			case core.Handoff:
				_, candidate := b.Body.(core.WorkerCandidate)
				_, none := b.Body.(core.WorkerNoCandidate)
				if said("\nCandidate: commit ") != candidate || said("\nIt produced no candidate.") != none {
					t.Fatalf("body %T, rendered %q", b.Body, got)
				}
				if said("\nOpen items: ") != (b.OpenItems != "") {
					t.Fatalf("open items %q, rendered %q", b.OpenItems, got)
				}
			case core.Report:
				for label, value := range map[string]string{
					"\nChanged: ": b.Changed, "\nIn flight: ": b.InFlight, "\nDecisions: ": b.Decisions,
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
		rec := records{}
		for _, m := range bodies(r, rec) {
			before := m
			first, err := prompts.RenderWith(m, rec)
			if err != nil {
				t.Fatal(err)
			}
			second, err := prompts.RenderWith(m, rec)
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
			if again, err := prompts.RenderWith(other, rec); err != nil || again != first {
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
		rec := records{}
		for _, m := range bodies(r, rec) {
			if idle, ok := m.Body.(core.LeadIdleReminder); ok {
				idle.Idle += time.Duration(r.IntN(1000)) * time.Millisecond
				m.Body = idle
			}
			if due, ok := m.Body.(core.ReportDue); ok {
				due.Every += time.Duration(r.IntN(1000)) * time.Millisecond
				m.Body = due
			}
			want, err := prompts.RenderWith(m, rec)
			if err != nil {
				t.Fatal(err)
			}
			back, err := wire.MailboxFromMessage(wire.MailboxMessage(m))
			if err != nil {
				t.Fatal(err)
			}
			if got, err := prompts.RenderWith(back, rec); err != nil || got != want {
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

// The reminder names the step the run is waiting on, from what the host counted, and always says
// how to finish.
func TestTheIdleReminderNamesTheNextStepFromTheCounts(t *testing.T) {
	r := rand.New(rand.NewPCG(15, 16))
	for range 2000 {
		body := core.LeadIdleReminder{Idle: time.Duration(r.IntN(7200)) * time.Second}
		switch r.IntN(4) {
		case 0:
		case 1:
			body.Subagents = 1 + r.Uint32N(1000)
		case 2:
			body.Workers = 1 + r.Uint32N(1000)
			body.Subagents = body.Workers + r.Uint32N(1000)
		case 3:
			body.Judges = 1 + r.Uint32N(1000)
			body.Workers = 1 + r.Uint32N(1000)
			body.Subagents = body.Workers + body.Judges
		}
		got, err := prompts.Render(core.Message{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		var want string
		switch {
		case body.Subagents == 0:
			want = "You have started no subagent"
		case body.Workers == 0:
			want = "No worker has been started"
		case body.Judges == 0:
			want = "No judge has been started"
		default:
			want = "Workers and judges have reported"
		}
		if !strings.Contains(got, want) {
			t.Fatalf("%+v rendered %q, want it to say %q", body, got, want)
		}
		if !strings.Contains(got, fmt.Sprintf("subagents %d, workers %d, judges %d", body.Subagents, body.Workers, body.Judges)) {
			t.Fatalf("%+v rendered %q without its counts", body, got)
		}
		for _, phrase := range []string{"`final`", "open plans and nothing outstanding"} {
			if !strings.Contains(got, phrase) {
				t.Fatalf("%+v rendered %q without %q", body, got, phrase)
			}
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

// The request for a report names the interval in whole minutes, says to post a progress report and not
// a final one, and tells a first report from a later one by the count the host holds.
func TestTheReportRequestNamesTheIntervalAndWhetherItIsTheFirstReport(t *testing.T) {
	r := rand.New(rand.NewPCG(17, 18))
	for range 2000 {
		body := core.ReportDue{
			Every: time.Duration(r.Int64N(int64(48*time.Hour))) + time.Duration(r.IntN(1000))*time.Microsecond, Reports: r.Uint32N(4),
		}
		got, err := prompts.Render(core.Message{Body: body})
		if err != nil {
			t.Fatal(err)
		}
		if want := fmt.Sprintf("about every %d minutes", int(body.Every/time.Minute)); !strings.Contains(got, want) {
			t.Fatalf("%+v rendered %q, want it to say %q", body, got, want)
		}
		for _, phrase := range []string{"`post_report`", "kind `progress`", "the whole run", "do not give a final report"} {
			if !strings.Contains(got, phrase) {
				t.Fatalf("%+v rendered %q without %q", body, got, phrase)
			}
		}
		if first := strings.Contains(got, "you have posted none yet"); first != (body.Reports == 0) {
			t.Fatalf("%+v rendered %q, which says it is a first report: %t", body, got, first)
		}
	}
}

// A request for a catch-up report states the check-in's frozen facts as they are, so the lead recomputes
// nothing, and shows times as Seattle's, never UTC, whatever zone the request's times are in.
func TestACatchUpRequestStatesTheFrozenFactsInSeattleTime(t *testing.T) {
	r := rand.New(rand.NewPCG(19, 20))
	seattle, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatal(err)
	}
	for range 2000 {
		rec := records{}
		since := time.Date(2026, time.Month(1+r.IntN(12)), 1+r.IntN(28), r.IntN(24), r.IntN(60), 0, 0, time.UTC)
		body := catchUp(r, rec, since)
		facts := rec["c1"].Outcome.(core.CheckInReady).Facts
		got, err := prompts.RenderWith(core.Message{ID: "m1", Body: body}, rec)
		if err != nil {
			t.Fatal(err)
		}
		for _, want := range []string{
			since.In(seattle).Format("2006-01-02 15:04 MST"), body.Until.In(seattle).Format("2006-01-02 15:04 MST"),
			fmt.Sprintf("Turns finished: %d", facts.Spend.Turns), fmt.Sprintf("%d started, %d ended", facts.AgentsStarted, facts.AgentsEnded),
			fmt.Sprintf("Measurements recorded: %d", facts.JobsMeasured), fmt.Sprintf("Reports you posted in it: %d", len(facts.Reports)),
		} {
			if !strings.Contains(got, want) {
				t.Fatalf("%+v rendered %q without %q", facts, got, want)
			}
		}
		if strings.Contains(got, "UTC") {
			t.Fatalf("rendered %q, which shows UTC", got)
		}
		if cost := facts.Spend.Usage.CostUSD; (cost != nil) != strings.Contains(got, ", costing $") {
			t.Fatalf("cost %v, rendered %q", cost, got)
		}
		for _, o := range facts.Objectives {
			if !strings.Contains(got, fmt.Sprintf("%g at the end (job %s, commit %s)", o.End.Value, o.End.Job, o.End.Commit)) {
				t.Fatalf("objective %+v missing from %q", o, got)
			}
		}
	}
}

// The words are made of the check-in's facts, so a request with none to read has no words: that is an
// error and not a prompt that tells the lead to work them out itself.
func TestACatchUpRequestWithNoFactsHasNoWords(t *testing.T) {
	m := core.Message{ID: "m1", Body: core.CatchUpRequest{CheckIn: "c1"}}
	for name, rec := range map[string]prompts.Records{
		"no records": nil, "no such check-in": records{},
		"a check-in still pending": records{"c1": {ID: "c1", Outcome: core.CheckInPending{}}},
		"a check-in that failed":   records{"c1": {ID: "c1", Outcome: core.CheckInFailed{Detail: "x"}}},
	} {
		if got, err := prompts.RenderWith(m, rec); !errors.Is(err, prompts.ErrNoFacts) {
			t.Errorf("%s: rendered %q (%v), want ErrNoFacts", name, got, err)
		}
	}
}
