// Package prompts words what an agent is sent for a message.
//
// A message the orchestrator addresses to an agent is typed data. The host turns it into the text
// the agent reads when it sends the command that carries it, and the process holding the agent is
// handed that text and nothing else: it never learns what a message was, which skill it names or
// which environment variable the words mention. Each message body has one template under
// templates/; Render passes data and nothing else, so no prompt text is built in code.
//
// Render is pure: the same message gives the same bytes on every call, which is what lets a
// delivery that is retried send the words the first attempt sent.
package prompts

import (
	"embed"
	"errors"
	"fmt"
	"strings"
	"text/template"
	"time"
	_ "time/tzdata" // Seattle's rules travel with the host: a prompt must not depend on the machine's zone files.
	"unicode"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

//go:embed templates/*.md.tmpl
var files embed.FS

// templates holds one template per message body, named by its file.
var templates = template.Must(template.New("prompts").Option("missingkey=error").ParseFS(files, "templates/*.md.tmpl"))

// ErrNoPrompt is returned for a message that has no body, so there is nothing to word.
var ErrNoPrompt = errors.New("the message has no prompt")

// ErrNoFacts is returned for a request whose words are made of a check-in's frozen facts when the check-in
// is not there or has none yet. The core sends the request in the commit that freezes them, so it cannot
// happen to a message the core made.
var ErrNoFacts = errors.New("the check-in a request is for has no facts")

// Records is what the words of some messages need besides the message: the check-in a catch-up request
// is worded from. core.State is one.
type Records interface {
	CheckIn(core.CheckInID) (core.CheckIn, bool)
}

// Render returns the words an agent is sent for m, for a message whose words need no records. It fails
// with ErrNoPrompt for a message with no body. A text or a brief of nothing but blanks renders to
// nothing, as it always has: the effect must still be carried out, because a turn that is never started
// would hold back its session.
func Render(m core.Message) (string, error) { return RenderWith(m, nil) }

// RenderWith is Render for any message, reading from records what its words are made of. The same
// message and the same records give the same bytes.
func RenderWith(m core.Message, records Records) (string, error) {
	name, data, err := dataOf(m, records)
	if err != nil {
		return "", err
	}
	var out strings.Builder
	if err := templates.ExecuteTemplate(&out, name+".md.tmpl", data); err != nil {
		return "", fmt.Errorf("render the %s prompt of message %s: %w", name, m.ID, err)
	}
	return strip(out.String()), nil
}

// strip removes the whitespace around text. It is the whitespace Python's str.strip removes, which
// the prompts were first rendered with, so the words stay byte for byte what agents have been sent:
// it is unicode.IsSpace and the four ASCII separators (0x1c to 0x1f) that Python counts and Go does
// not.
func strip(text string) string {
	return strings.TrimFunc(text, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	})
}

// The data each template is given. A field is a thing the words say, spelled as a word.
type (
	textData struct{ Text string }

	leadBriefData struct {
		Task        string
		FanoutLimit uint32
		Steering    string
	}

	// handoffData holds the fields of every kind of handoff; Kind says which the template reads.
	handoffData struct {
		From                                                 core.AgentID
		PlanNumber                                           uint32
		Kind                                                 string
		Claim, Learned, Next, OpenItems                      string
		Commit, ParentCommit, Revision                       core.Commit
		Branch, Checks, Measurement, Blocker                 string
		Plan, Confidence, Unestablished, DecidingMeasurement string
		Verdict                                              core.Verdict
		Reasons, Applied, Unapplied                          string
		Question, Answer, Evidence, Unsupported, Informs     string
	}

	reportData struct {
		From      core.AgentID
		Headline  string
		Changed   string
		InFlight  string
		Decisions string
	}

	jobFinishedData struct {
		Job     core.JobID
		Outcome core.JobState
		Detail  string
	}

	subagentSettledData struct {
		Agent  core.AgentID
		Status core.AgentStatus
		Detail string
	}

	// runLimitReachedData says which limit the run reached and what is left to say about it. HasSpent
	// is false when no turn has reported a cost.
	runLimitReachedData struct {
		Limit          core.RunLimit
		ElapsedMinutes uint32
		HasSpent       bool
		Spent          float64
		MaxCost        float64
		GraceMinutes   uint32
	}

	leadIdleReminderData struct {
		IdleMinutes uint32
		Subagents   uint32
		Workers     uint32
		Judges      uint32
	}

	// catchUpRequestData is the window the operator wants a report on, as times of day in Seattle (how
	// the operator reads them) and its length in whole seconds and whole minutes: the words say seconds
	// for a window of a minute or two. Facts are what the host worked out for the window and froze.
	catchUpRequestData struct {
		Since, Until     string
		Seconds, Minutes uint32
		Facts            catchUpFacts
	}

	// catchUpFacts is a check-in's facts, as the words state them. A price no turn reported is absent:
	// HasCost says whether CostUSD is a figure.
	catchUpFacts struct {
		Turns, Unpriced                          uint32
		HasCost                                  bool
		CostUSD                                  float64
		AgentsStarted, AgentsEnded, JobsMeasured uint32
		Objectives                               []catchUpObjective
		PlansCreated, PlansChanged               []uint32
		Reports                                  int
	}

	// catchUpObjective is where one objective stood at the two ends of the window. HasStart is false
	// when nothing was measured before the window.
	catchUpObjective struct {
		Metric, Direction string
		HasStart          bool
		Start, End        catchUpBest
	}

	// catchUpBest is a best value, the measure job and the commit that made it.
	catchUpBest struct {
		Value       float64
		Job, Commit string
	}

	// reportDueData is how often the operator expects a report and how many the lead has posted.
	reportDueData struct {
		EveryMinutes uint32
		Reports      uint32
	}
)

// dataOf names the template of m's body and gathers the data it renders.
func dataOf(m core.Message, records Records) (string, any, error) {
	switch b := m.Body.(type) {
	case core.Text:
		return "text", textData{Text: b.Text}, nil
	case core.Brief:
		return "brief", textData{Text: b.Text}, nil
	case core.LeadBrief:
		return "lead_brief", leadBriefData(b), nil
	case core.Handoff:
		return "handoff", handoffDataOf(m.From, b), nil
	case core.Report:
		return "report", reportData{
			From: m.From, Headline: b.Headline, Changed: b.Changed, InFlight: b.InFlight,
			Decisions: b.Decisions,
		}, nil
	case core.JobFinished:
		return "job_finished", jobFinishedData{Job: b.Job, Outcome: b.Outcome, Detail: b.Detail}, nil
	case core.SubagentSettled:
		return "subagent_settled", subagentSettledData{Agent: b.Agent, Status: b.Status, Detail: b.Detail}, nil
	case core.HandoffReminder:
		return "handoff_reminder", struct{}{}, nil
	case core.RunLimitReached:
		d := runLimitReachedData{
			Limit: b.Limit, ElapsedMinutes: uint32(b.Elapsed / time.Minute), MaxCost: b.MaxCost, GraceMinutes: uint32(b.Grace / time.Minute),
		}
		if b.Spent != nil {
			d.HasSpent, d.Spent = true, *b.Spent
		}
		return "run_limit_reached", d, nil
	case core.ReportDue:
		// Whole seconds in a uint32, as the message is stored, like the idle reminder's.
		return "report_due", reportDueData{EveryMinutes: uint32(b.Every/time.Second) / 60, Reports: b.Reports}, nil
	case core.CatchUpRequest:
		facts, err := catchUpFactsOf(m, b, records)
		if err != nil {
			return "", nil, err
		}
		span := b.Until.Sub(b.Since)
		return "catch_up_request", catchUpRequestData{
			Since: seattle(b.Since), Until: seattle(b.Until), Seconds: uint32(span / time.Second), Minutes: uint32(span / time.Minute),
			Facts: facts,
		}, nil
	case core.LeadIdleReminder:
		// Whole seconds in a uint32, as the message is stored, so the minutes do not depend on
		// whether the message was read back or is still the one that was built.
		seconds := uint32(b.Idle / time.Second)
		return "lead_idle_reminder", leadIdleReminderData{
			IdleMinutes: seconds / 60, Subagents: b.Subagents, Workers: b.Workers, Judges: b.Judges,
		}, nil
	}
	return "", nil, fmt.Errorf("message %s has no body: %w", m.ID, ErrNoPrompt)
}

func handoffDataOf(from core.AgentID, h core.Handoff) handoffData {
	d := handoffData{From: from, PlanNumber: h.PlanNumber, Claim: h.Claim, Learned: h.Learned, Next: h.Next, OpenItems: h.OpenItems}
	switch b := h.Body.(type) {
	case core.WorkerCandidate:
		d.Kind, d.Commit, d.ParentCommit, d.Branch, d.Checks, d.Measurement = "candidate", b.Commit, b.ParentCommit, b.Branch, b.Checks, b.Measurement
	case core.WorkerNoCandidate:
		d.Kind, d.Blocker = "no_candidate", b.Blocker
	case core.PlannerPlan:
		d.Kind, d.Plan, d.Confidence, d.Unestablished, d.DecidingMeasurement = "plan", b.Plan, b.Confidence, b.Unestablished, b.DecidingMeasurement
	case core.JudgeFinding:
		d.Kind, d.Commit, d.Verdict, d.Reasons = "verdict", b.Commit, b.Verdict, b.Reasons
	case core.CombinerResult:
		d.Kind, d.Commit, d.Branch, d.Applied, d.Unapplied, d.Checks, d.Measurement = "combined", b.Commit, b.Branch, b.Applied, b.Unapplied, b.Checks, b.Measurement
	case core.ProfilerFindings:
		d.Kind, d.Question, d.Revision, d.Answer, d.Evidence, d.Unsupported, d.Informs = "findings", b.Question, b.Revision, b.Answer, b.Evidence, b.Unsupported, b.Informs
	}
	return d
}

// operatorZone is where the operator reads times: Seattle. A prompt never shows UTC.
var operatorZone = func() *time.Location {
	zone, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		panic("prompts: no time zone data for Seattle: " + err.Error())
	}
	return zone
}()

// seattle words a time of day, with its date and the zone's name, as the operator reads it.
func seattle(t time.Time) string { return t.In(operatorZone).Format("2006-01-02 15:04 MST") }

// catchUpFactsOf reads the facts a catch-up request is worded from out of its check-in.
func catchUpFactsOf(m core.Message, b core.CatchUpRequest, records Records) (catchUpFacts, error) {
	if records == nil {
		return catchUpFacts{}, fmt.Errorf("message %s asks for a catch-up report and no records were given: %w", m.ID, ErrNoFacts)
	}
	check, ok := records.CheckIn(b.CheckIn)
	if !ok {
		return catchUpFacts{}, fmt.Errorf("message %s is for check-in %s, which does not exist: %w", m.ID, b.CheckIn, ErrNoFacts)
	}
	ready, ok := check.Outcome.(core.CheckInReady)
	if !ok {
		return catchUpFacts{}, fmt.Errorf("message %s is for check-in %s, which is %T: %w", m.ID, b.CheckIn, check.Outcome, ErrNoFacts)
	}
	f := ready.Facts
	out := catchUpFacts{
		Turns: f.Spend.Turns, Unpriced: f.Spend.Unpriced,
		AgentsStarted: f.AgentsStarted, AgentsEnded: f.AgentsEnded, JobsMeasured: f.JobsMeasured,
		PlansCreated: f.PlansCreated, PlansChanged: f.PlansChanged, Reports: len(f.Reports),
	}
	if c := f.Spend.Usage.CostUSD; c != nil {
		out.HasCost, out.CostUSD = true, *c
	}
	best := func(b *core.Best) catchUpBest {
		return catchUpBest{Value: b.Value, Job: string(b.Job), Commit: string(b.Commit)}
	}
	for _, o := range f.Objectives {
		move := catchUpObjective{Metric: o.Metric, Direction: string(o.Direction)}
		if o.Start != nil {
			move.HasStart, move.Start = true, best(o.Start)
		}
		if o.End != nil {
			move.End = best(o.End)
		}
		out.Objectives = append(out.Objectives, move)
	}
	return out, nil
}
