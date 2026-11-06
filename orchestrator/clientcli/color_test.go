package clientcli_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// --- what the renderer's sequences are ---

// sgr is the one kind of escape sequence the renderer writes: select graphic rendition.
var sgr = regexp.MustCompile("\x1b\\[([0-9;]*)m")

func stripSGR(s string) string { return sgr.ReplaceAllString(s, "") }

// escaped shows the renderer's escape bytes as \e, so that a golden file can be read.
func escaped(s string) string { return strings.ReplaceAll(s, "\x1b", `\e`) }

// styled checks one drawn line: it holds only sequences the renderer writes, which use no colour but the
// 16 standard ones and dim and bold, never a background; every span is closed; and a line that holds any
// ends reset.
func styled(t testing.TB, label, line string) {
	t.Helper()
	opened, closed := 0, 0
	for _, m := range sgr.FindAllStringSubmatch(line, -1) {
		if m[1] == "0" {
			closed++
			continue
		}
		opened++
		for _, code := range strings.Split(m[1], ";") {
			switch code {
			case "1", "2", "31", "32", "33", "36":
			default:
				t.Fatalf("%s: code %q is not dim, bold or one of the colours used: %q", label, code, line)
			}
		}
	}
	if rest := stripSGR(line); strings.ContainsRune(rest, 0x1b) || strings.ContainsRune(rest, 0x9b) {
		t.Fatalf("%s: an escape that is not the renderer's: %q", label, line)
	}
	if closed < opened {
		t.Fatalf("%s: %d spans opened and %d closed: %q", label, opened, closed, line)
	}
	if opened > 0 && !strings.HasSuffix(line, attachview.Reset) {
		t.Fatalf("%s: the line does not end reset: %q", label, line)
	}
}

// --- the lines the model makes ---

// modelLines is what the model prints over a random sequence of items of the viewed agent with hostile
// text, changes of the run of every tone, and the agent list.
func modelLines(t testing.TB, seed uint64) []attachview.Line {
	r := rand.New(rand.NewPCG(seed, 51))
	s := &service{r: r, store: map[string][]*pb.Item{}}
	v, _ := s.world()
	var items []*pb.Item
	for range 3 + r.IntN(8) {
		items = append(items, s.item(s.agents[0], pb.ItemStatus_ITEM_STATUS_FINISHED))
	}
	m, outs := attachview.Begin(attachview.Options{Controls: true, History: 200, Width: 40 + r.IntN(80), Now: epoch, Zone: time.UTC}, v, nil)
	var lines []attachview.Line
	take := func(outs []attachview.Output) {
		for _, o := range outs {
			if p, ok := o.(attachview.Print); ok {
				lines = append(lines, p.Lines...)
			}
		}
	}
	take(outs)
	m, outs = attachview.Update(m, attachview.AgentHistory{Gen: subscribeOf(outs).Gen, Items: items})
	take(outs)

	bad := agentOf("run-1", "a1", pb.AgentRole_AGENT_ROLE_PLANNER, pb.AgentStatus_AGENT_STATUS_FAILED, 1)
	bad.EndDetail = hostile[r.IntN(len(hostile))]
	handoff := &pb.Message{Id: "h", RunId: "run-1", FromAgentId: "a2", ToAgentId: s.agents[0], At: at(3),
		Body: &pb.Message_Handoff{Handoff: &pb.Handoff{Claim: hostile[r.IntN(len(hostile))]}}}
	jobs := []*pb.Message{}
	for i, outcome := range []pb.JobOutcome{pb.JobOutcome_JOB_OUTCOME_SUCCEEDED, pb.JobOutcome_JOB_OUTCOME_FAILED, pb.JobOutcome_JOB_OUTCOME_CANCELLED} {
		jobs = append(jobs, &pb.Message{Id: fmt.Sprintf("j%d", i), RunId: "run-1", ToAgentId: s.agents[0], At: at(4 + i),
			Body: &pb.Message_JobFinished{JobFinished: &pb.JobFinished{JobId: fmt.Sprintf("job-%d", i), Outcome: outcome, Detail: hostile[r.IntN(len(hostile))]}}})
	}
	final := reportOf("run-1", "r", hostile[r.IntN(len(hostile))], 8, 1, pb.ReportKind_REPORT_KIND_FINAL)
	limit := &pb.Message{Id: "l", RunId: "run-1", ToAgentId: s.agents[0], At: at(9), Body: &pb.Message_RunLimitReached{RunLimitReached: &pb.RunLimitReached{Limit: pb.RunLimit_RUN_LIMIT_COST}}}
	change := func(seq uint64, minutes int, body func(*pb.RunChange)) *pb.RunChange {
		ev := &pb.RunChange{Sequence: seq, RunId: "run-1", At: at(minutes)}
		body(ev)
		return ev
	}
	message := func(seq uint64, minutes int, msg *pb.Message) *pb.RunChange {
		return change(seq, minutes, func(ev *pb.RunChange) { ev.Body = &pb.RunChange_Message{Message: msg} })
	}
	frames := []*pb.RunChange{
		change(2, 2, func(ev *pb.RunChange) { ev.Body = &pb.RunChange_Agent{Agent: bad} }),
		message(3, 3, handoff), message(7, 8, final), message(8, 9, limit),
	}
	for i, j := range jobs {
		frames = append(frames, message(uint64(4+i), 4+i, j))
	}
	for _, f := range frames {
		m, outs = attachview.Update(m, attachview.RunFrame{Change: f})
		take(outs)
	}
	for _, k := range attachview.ParseKeys([]byte("a")) {
		m, outs = attachview.Update(m, k)
		take(outs)
	}
	return lines
}

// With the sequences the renderer writes taken out, coloured output is the plain output, line for line:
// colour can never change what a line says or where it is cut. Whatever an agent or a tool said, the
// only escapes in the output are the renderer's, each span closes, and every styled line ends reset.
func TestColourNeverChangesAnythingButStyle(t *testing.T) {
	styledLines := 0
	for seed := range uint64(200) {
		for _, l := range modelLines(t, seed) {
			for _, width := range []int{0, 8, 23, 40, 80, 133} {
				plain, colour := attachview.Render(l, width), attachview.RenderColor(l, width)
				if got := stripSGR(colour); got != plain {
					t.Fatalf("seed %d width %d: stripped colour %q, plain %q (line %+v)", seed, width, got, plain, l)
				}
				styled(t, fmt.Sprintf("seed %d width %d", seed, width), colour)
				if colour != plain {
					styledLines++
				}
				if strings.ContainsRune(plain, 0x1b) {
					t.Fatalf("seed %d: the plain line holds an escape: %q", seed, plain)
				}
			}
		}
	}
	if styledLines == 0 {
		t.Fatal("no line was styled")
	}
}

// A line that reaches the renderer with escapes in it, as no line from the model does, still cannot
// bring one out that is not the renderer's.
func TestColourTakesNoEscapeFromTheText(t *testing.T) {
	for _, kind := range []attachview.LineKind{attachview.LineMilestone, attachview.LineTurn, attachview.LineHeader, attachview.LineNotice, attachview.LinePicker,
		attachview.LineAssistant, attachview.LineAssistantMore, attachview.LinePrompt, attachview.LineTool, attachview.LineToolFailure} {
		for _, text := range hostile {
			for _, tone := range []attachview.Tone{attachview.ToneNone, attachview.ToneGood, attachview.ToneBad, attachview.ToneWarn, attachview.ToneStrong} {
				l := attachview.Line{Kind: kind, Label: text, Text: text + " lead " + text, Subject: "lead", Mark: text, Tone: tone}
				for _, width := range []int{0, 12, 60} {
					styled(t, fmt.Sprintf("kind %d tone %d %q", kind, tone, text), attachview.RenderColor(l, width))
				}
			}
		}
	}
	if got := attachview.StyleFooter("x\x1b[2Jy\x1b]0;t\x07"); got != "\x1b[2mxy\x1b[0m" {
		t.Errorf("a footer with escapes in it drew %q", got)
	}
}

// Colour does not change where a line is cut: whatever the width, the characters shown are the plain
// line's.
func TestColourDoesNotMoveTheCut(t *testing.T) {
	l := attachview.Line{Kind: attachview.LineTurn, Subject: "worker-1", Mark: "turn 2 failed", Tone: attachview.ToneBad,
		Text: "03:00:00  worker-1 turn 2 failed, 3 tool calls, cost $0.0300, 1.0k out"}
	for width := 8; width <= 90; width++ {
		plain, colour := attachview.Render(l, width), attachview.RenderColor(l, width)
		if stripSGR(colour) != plain {
			t.Fatalf("width %d: %q is not %q", width, stripSGR(colour), plain)
		}
	}
}

// --- a golden for each style ---

func TestEachStyleHasItsLook(t *testing.T) {
	styles := []struct {
		name string
		line attachview.Line
	}{
		{"assistant text is the terminal's own", attachview.Line{Kind: attachview.LineAssistant, Text: "The planner proposes plan 1."}},
		{"assistant text, a later line", attachview.Line{Kind: attachview.LineAssistantMore, Text: "A worker will build it."}},
		{"tool call", attachview.Line{Kind: attachview.LineTool, Text: "$ pytest -x tests/"}},
		{"tool call that failed", attachview.Line{Kind: attachview.LineTool, Text: "$ pytest -x tests/", Tone: attachview.ToneBad}},
		{"what went wrong", attachview.Line{Kind: attachview.LineToolFailure, Text: "failed (exit 1): KeyError: 'ab'", Tone: attachview.ToneBad}},
		{"prompt", attachview.Line{Kind: attachview.LinePrompt, Label: "steer", Text: "Try the cache first."}},
		{"turn started", attachview.Line{Kind: attachview.LineTurn, Subject: "lead", Text: "03:00:00  lead turn 1 started: its brief"}},
		{"turn completed", attachview.Line{Kind: attachview.LineTurn, Subject: "lead", Text: "03:01:00  lead turn 1 completed, 3 tool calls, cost $0.0421, 1.2k out"}},
		{"turn failed", attachview.Line{Kind: attachview.LineTurn, Subject: "lead", Mark: "turn 2 failed", Tone: attachview.ToneBad, Text: "03:02:00  lead turn 2 failed, 1 tool call"}},
		{"milestone", attachview.Line{Kind: attachview.LineMilestone, Text: "03:01:00  planner-1 started (claude_code)"}},
		{"milestone, a handoff", attachview.Line{Kind: attachview.LineMilestone, Mark: "planner-1 handed off", Tone: attachview.ToneGood, Text: "03:05:00  planner-1 handed off: plan 1"}},
		{"milestone, a job that succeeded", attachview.Line{Kind: attachview.LineMilestone, Mark: "job j1 succeeded", Tone: attachview.ToneGood, Text: "03:06:00  job j1 succeeded"}},
		{"milestone, a measurement", attachview.Line{Kind: attachview.LineMilestone, Mark: "measured 1a2b3c4d", Tone: attachview.ToneGood, Text: "03:07:00  measured 1a2b3c4d: tokens_per_s = 1200"}},
		{"milestone, an agent that failed", attachview.Line{Kind: attachview.LineMilestone, Mark: "worker-1 failed", Tone: attachview.ToneBad, Text: "03:08:00  worker-1 failed: out of memory"}},
		{"milestone, a limit reached", attachview.Line{Kind: attachview.LineMilestone, Mark: "reached its cost limit", Tone: attachview.ToneWarn, Text: "03:09:00  lead was told the run reached its cost limit and to finish"}},
		{"milestone, a final report", attachview.Line{Kind: attachview.LineMilestone, Tone: attachview.ToneStrong, Text: "03:10:00  lead reported (final, number 2): 9% faster"}},
		{"header", attachview.Line{Kind: attachview.LineHeader, Subject: "worker-1", Text: "-- worker-1: worker, ended, started by lead (id agent-9f01b7d4e5a2) --"}},
		{"notice", attachview.Line{Kind: attachview.LineNotice, Text: "agents (type a number, Enter to open it, Esc to cancel):"}},
		{"agent list, an agent that is going", attachview.Line{Kind: attachview.LinePicker, Subject: "lead", Mark: "active", Text: "   1  lead         lead      active   agent-1"}},
		{"agent list, an agent that failed", attachview.Line{Kind: attachview.LinePicker, Subject: "worker-1", Mark: "failed", Tone: attachview.ToneBad, Text: "   3  worker-1     worker    failed   agent-3"}},
	}
	var b strings.Builder
	for _, s := range styles {
		got := attachview.RenderColor(s.line, 100)
		styled(t, s.name, got)
		fmt.Fprintf(&b, "%s\n    %s\n", s.name, escaped(got))
	}
	footer := attachview.StyleFooter("0:12 | 2 active | $0.07 | viewing lead | a agents, Ctrl-C detaches")
	styled(t, "footer", footer)
	fmt.Fprintf(&b, "footer\n    %s\n", escaped(footer))
	checkGolden(t, "color_styles.golden", b.String())
}

// --- the view at a terminal ---

// At a terminal that wants colour the view is styled, and with the styles taken out it is the plain
// view, byte for byte.
func TestAttachColoursTheViewAtATerminalAndNothingElseChanges(t *testing.T) {
	w, screen, _ := terminalWorld(t, conversation(pb.RunStatus_RUN_STATUS_ENDED))
	screen.refusesRawInput = true // plain lines at a terminal: no keys, no footer, so the output is exact.
	w.color = true
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	checkGolden(t, "attach_lead_color.golden", escaped(got.stdout))
	plain, err := os.ReadFile(filepath.Join("testdata", "attach_lead.golden"))
	if err != nil {
		t.Fatal(err)
	}
	if stripSGR(got.stdout) != string(plain) {
		t.Errorf("the coloured view is not the plain view with styles:\n%s", stripSGR(got.stdout))
	}
	for i, line := range strings.Split(strings.TrimSuffix(got.stdout, attachview.Reset), "\n") {
		styled(t, fmt.Sprintf("line %d", i), line)
	}
}

// With a terminal's keys and footer, the footer is dim, and the last thing written puts the style back.
func TestAttachAtATerminalEndsWithTheStyleReset(t *testing.T) {
	w, _, _ := terminalWorld(t, conversation(pb.RunStatus_RUN_STATUS_ENDED))
	w.color = true
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	if !strings.HasSuffix(got.stdout, attachview.Reset) {
		t.Errorf("the view ended without a reset: %q", got.stdout[max(0, len(got.stdout)-60):])
	}
	if !regexp.MustCompile("\x1b\\[2m[^\x1b]*viewing lead[^\x1b]*\x1b\\[0m").MatchString(got.stdout) {
		t.Errorf("the footer is not drawn dim:\n%q", got.stdout)
	}
	w, _, _ = terminalWorld(t, conversation(pb.RunStatus_RUN_STATUS_ENDED))
	if plain := w.run(context.Background(), "run", "attach", "run-1"); sgr.MatchString(plain.stdout) {
		t.Errorf("without colour the view still wrote a style: %q", plain.stdout)
	}
}

// No escape byte at all comes out when there is no terminal, whatever the environment says, or when the
// environment does not want colour (main works that out; see the client's own test), and there is no flag.
func TestNoEscapeComesOutWithoutATerminalOrWithoutColour(t *testing.T) {
	cases := []struct {
		name            string
		terminal, color bool
	}{
		{"no terminal, colour wanted", false, true},
		{"no terminal, no colour", false, false},
		{"a terminal that does not want colour", true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := conversation(pb.RunStatus_RUN_STATUS_ENDED)
			w, screen, _ := terminalWorld(t, f)
			w.terminal, w.color = c.terminal, c.color
			screen.refusesRawInput = true // plain lines even at a terminal.
			if !c.terminal {
				w.screen = nil
			}
			for _, args := range [][]string{{"run", "attach", "run-1"}, {"run", "attach", "run-1", "--agent", "worker-1"}} {
				got := w.run(context.Background(), args...)
				if got.code != 0 {
					t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
				}
				if strings.ContainsRune(got.stdout, 0x1b) {
					t.Errorf("%v wrote an escape: %q", args, got.stdout)
				}
				want, err := os.ReadFile(filepath.Join("testdata", map[bool]string{true: "attach_worker.golden", false: "attach_lead.golden"}[len(args) > 3]))
				if err != nil {
					t.Fatal(err)
				}
				if got.stdout != string(want) {
					t.Errorf("%v: the bytes changed:\n%s", args, got.stdout)
				}
			}
		})
	}
}
