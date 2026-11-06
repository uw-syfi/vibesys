package clientcli_test

import (
	"fmt"
	"math/rand/v2"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"
	"unicode"
	"unicode/utf8"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// --- a run to look at ---

// A world is a run with a lead, a planner and a worker whose parent is the planner.
func conversationWorld(status pb.RunStatus) (*attachview.View, []*pb.Agent) {
	run := runOf("run-1", "/work/repo", status, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)
	planner := agentOf("run-1", "planner-id", pb.AgentRole_AGENT_ROLE_PLANNER, pb.AgentStatus_AGENT_STATUS_ENDED, 1)
	planner.ParentAgentId = lead.Id
	worker := agentOf("run-1", "worker-id", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ACTIVE, 2)
	worker.ParentAgentId = planner.Id
	agents := []*pb.Agent{lead, planner, worker}
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: agents, Sequence: 1})
	return v, agents
}

func assistantItem(agent string, ordinal uint32, turn, text string) *pb.Item {
	return &pb.Item{Id: fmt.Sprintf("%s-%d", agent, ordinal), TurnId: turn, AgentId: agent, Ordinal: ordinal, Status: pb.ItemStatus_ITEM_STATUS_FINISHED,
		At: at(int(ordinal)), Body: &pb.Item_Assistant{Assistant: &pb.Text{Text: text}}}
}

func toolItem(agent string, ordinal uint32, turn string, call *pb.ToolCall) *pb.Item {
	return &pb.Item{Id: fmt.Sprintf("%s-%d", agent, ordinal), TurnId: turn, AgentId: agent, Ordinal: ordinal, Status: pb.ItemStatus_ITEM_STATUS_FINISHED,
		At: at(int(ordinal)), Body: &pb.Item_Tool{Tool: call}}
}

func shell(cmd string) *pb.ToolCall {
	return &pb.ToolCall{Call: &pb.ToolCall_Shell{Shell: &pb.ShellCall{Command: cmd}}}
}

// shown runs the view from its start to the replay of items, and returns every line it printed.
func shown(t testing.TB, v *attachview.View, agent string, width int, items ...*pb.Item) []string {
	t.Helper()
	m, outs := attachview.Begin(attachview.Options{Start: agent, History: attachview.DefaultHistory, Width: width, Zone: time.UTC}, v, nil)
	lines := printedAt(outs, width)
	_, outs = attachview.Update(m, attachview.AgentHistory{Gen: subscribeOf(outs).Gen, Items: items})
	return append(lines, printedAt(outs, width)...)
}

// subscribeOf is the subscription the outputs open.
func subscribeOf(outs []attachview.Output) attachview.Subscribe {
	for _, o := range outs {
		if s, ok := o.(attachview.Subscribe); ok {
			return s
		}
	}
	return attachview.Subscribe{}
}

// printed is the lines the outputs print, as they are drawn at the default width.
func printed(outs []attachview.Output) []string { return printedAt(outs, 0) }

// printedAt is the lines the outputs print, as they are drawn in a terminal width wide.
func printedAt(outs []attachview.Output, width int) []string {
	var lines []string
	for _, o := range outs {
		if p, ok := o.(attachview.Print); ok {
			for _, l := range p.Lines {
				lines = append(lines, attachview.Render(l, width))
			}
		}
	}
	return lines
}

// --- how each kind of tool call is abridged ---

func TestToolCallsAreShownAbridgedOneLineEach(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_ENDED)
	call := func(c *pb.ToolCall) *pb.ToolCall { return c }
	failed := func(c *pb.ToolCall, exit int32, out string) *pb.ToolCall {
		c.ExitCode, c.Output, c.IsError = &exit, out, true
		return c
	}
	cases := []struct {
		name  string
		call  *pb.ToolCall
		width int
		want  []string
	}{
		{"shell", shell("go test ./..."), 100, []string{"  $ go test ./..."}},
		{"shell keeps only the first line", shell("cd src\nmake all\n"), 100, []string{"  $ cd src ..."}},
		{"shell skips blank lines", shell("\n\n  ls -la\n"), 100, []string{"  $ ls -la"}},
		{"shell not yet known", shell(""), 100, []string{"  $ (command not yet known)"}},
		{"read", call(&pb.ToolCall{Call: &pb.ToolCall_Read{Read: &pb.ReadCall{Target: "src/a.go"}}}), 100, []string{"  read src/a.go"}},
		{"write", call(&pb.ToolCall{Call: &pb.ToolCall_Write{Write: &pb.WriteCall{Target: "out/b.txt"}}}), 100, []string{"  write out/b.txt"}},
		{"edit lists its files", call(&pb.ToolCall{Call: &pb.ToolCall_Edit{Edit: &pb.EditCall{Targets: []string{"a.go", "b.go"}}}}), 100, []string{"  edit a.go, b.go"}},
		{"search", call(&pb.ToolCall{Call: &pb.ToolCall_Search{Search: &pb.SearchCall{Pattern: "TODO.*fix"}}}), 100, []string{"  search TODO.*fix"}},
		{"other shows its name and first line", call(&pb.ToolCall{Call: &pb.ToolCall_Other{Other: &pb.OtherCall{Name: "web_fetch", Text: "url: https://example.test\nmore"}}}), 100, []string{"  web_fetch url: https://example.test ..."}},
		{"output of a call that worked is hidden", call(&pb.ToolCall{Call: &pb.ToolCall_Shell{Shell: &pb.ShellCall{Command: "ls"}}, Output: "a\nb\nc"}), 100, []string{"  $ ls"}},
		{"a failure shows its exit code and one line", failed(shell("make"), 2, "compiling\nmake: *** [all] Error 2\n\n"), 100,
			[]string{"  $ make", "    failed (exit 2): make: *** [all] Error 2"}},
		{"a failure without an exit code", failed(&pb.ToolCall{Call: &pb.ToolCall_Read{Read: &pb.ReadCall{Target: "nope"}}}, 0, "no such file"), 100,
			[]string{"  read nope", "    failed (exit 0): no such file"}},
		{"clipped at the width", shell("go test -run TestSomethingWithALongName ./pkg/..."), 30, []string{"  $ go test -run TestSometh..."}},
		{"a failure is clipped too", failed(shell("x"), 1, "a very long message about what went wrong in there"), 24, []string{"  $ x", "    failed (exit 1): ..."}},
		{"a very narrow terminal still gets something", shell("abcdefghijklmnop"), 10, []string{"  $ abc..."}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := shown(t, v, "", c.width, toolItem("lead-run-1", 1, "turn-1", c.call))
			var tool []string
			for _, line := range got {
				if strings.HasPrefix(line, "  ") {
					tool = append(tool, line)
				}
			}
			if !slices.Equal(tool, c.want) {
				t.Errorf("got %q, want %q", tool, c.want)
			}
		})
	}
}

func TestAssistantTextIsShownInFullAndReasoningNotAtAll(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_ENDED)
	long := strings.Repeat("word ", 80)
	reasoning := &pb.Item{Id: "r", TurnId: "turn-1", AgentId: "lead-run-1", Ordinal: 2, Status: pb.ItemStatus_ITEM_STATUS_FINISHED, At: at(2), Body: &pb.Item_Reasoning{Reasoning: &pb.Text{Text: "secret thoughts"}}}
	got := shown(t, v, "", 20, assistantItem("lead-run-1", 1, "turn-1", "first line\nsecond line\n\n"+long), reasoning)
	text := strings.Join(got, "\n")
	for _, want := range []string{"* first line", "  second line", strings.TrimSpace(long)} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in\n%s", want, text)
		}
	}
	if strings.Contains(text, "secret thoughts") {
		t.Errorf("reasoning was shown:\n%s", text)
	}
}

func TestPromptsAreLabelledByWhatStartedTheTurnAndClipped(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)
	turns := []*pb.Turn{
		{Id: "t1", AgentId: lead.Id, Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_BRIEF, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(0), EndedAt: at(1)},
		{Id: "t2", AgentId: lead.Id, Ordinal: 2, Trigger: pb.TurnTrigger_TURN_TRIGGER_OPERATOR, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(2), EndedAt: at(3)},
	}
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: []*pb.Agent{lead}, Turns: turns, Sequence: 1})
	prompt := func(ord uint32, turn, text string) *pb.Item {
		return &pb.Item{Id: turn + "p", TurnId: turn, AgentId: lead.Id, Ordinal: ord, Status: pb.ItemStatus_ITEM_STATUS_FINISHED, At: at(int(ord)), Body: &pb.Item_Prompt{Prompt: &pb.Text{Text: text}}}
	}
	got := shown(t, v, "", 40, prompt(1, "t1", "optimise the scheduler\nplease"), prompt(2, "t2", "try the second plan, and report back with numbers"))
	want := []string{
		"03:00:00  lead turn 1 started: its brief",
		"  [brief] optimise the scheduler please",
		"03:01:00  lead turn 1 completed, 0 tool calls",
		"03:02:00  lead turn 2 started: your steer",
		"  [steer] try the second plan, and re...",
		"03:03:00  lead turn 2 completed, 0 tool calls",
	}
	if !slices.Equal(got, want) {
		t.Errorf("got\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
}

func TestTurnsSayWhatStartedThemAndNeverABareInputTokenFigure(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)
	worker := agentOf("run-1", "w", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ENDED, 1)
	handoff := &pb.Message{Id: "m1", RunId: "run-1", FromAgentId: "w", ToAgentId: lead.Id, At: at(2), Body: &pb.Message_Handoff{Handoff: &pb.Handoff{Claim: "done"}}}
	usage := &pb.Usage{InputTokens: ptr(uint64(1_500_000)), CacheReadTokens: ptr(uint64(9)), OutputTokens: ptr(uint64(4200)), CostUsd: ptr(0.5)}
	turns := []*pb.Turn{
		{Id: "t1", AgentId: lead.Id, Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_HANDOFF, TriggerMessageId: "m1", Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(3), EndedAt: at(4), Usage: usage},
		{Id: "t2", AgentId: lead.Id, Ordinal: 2, Trigger: pb.TurnTrigger_TURN_TRIGGER_REMINDER, Status: pb.TurnStatus_TURN_STATUS_INTERRUPTED, StartedAt: at(5), EndedAt: at(6)},
	}
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: []*pb.Agent{lead, worker}, Turns: turns, Messages: []*pb.Message{handoff}, Sequence: 1})
	got := strings.Join(shown(t, v, "", 100), "\n")
	for _, want := range []string{"lead turn 1 started: worker-1 finished", "lead turn 1 completed, 0 tool calls, cost $0.5000, 4.2k out", "lead turn 2 started: a reminder", "lead turn 2 interrupted, 0 tool calls"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in\n%s", want, got)
		}
	}
	if strings.Contains(got, "1.50M") || strings.Contains(got, " in,") || strings.Contains(got, "cached") {
		t.Errorf("a bare input-token figure was shown:\n%s", got)
	}
}

// --- names ---

func TestAgentsAreNamedByRoleInTheOrderTheyStarted(t *testing.T) {
	v, agents := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	for id, want := range map[string]string{agents[0].Id: "lead", "planner-id": "planner-1", "worker-id": "worker-1", "nobody": "nobody"} {
		if got := v.AgentName(id); got != want {
			t.Errorf("AgentName(%q) = %q, want %q", id, got, want)
		}
	}
	more := agentOf("run-1", "planner-b", pb.AgentRole_AGENT_ROLE_PLANNER, pb.AgentStatus_AGENT_STATUS_ACTIVE, 3)
	v.Load(&pb.RunsSnapshot{Agents: []*pb.Agent{more}})
	if got := v.AgentName("planner-b"); got != "planner-2" {
		t.Errorf("the second planner is %q", got)
	}
	if got := v.AgentName("planner-id"); got != "planner-1" {
		t.Errorf("a later agent renamed the first planner to %q", got)
	}
}

func TestResolveAgentTakesANameAnIdOrAUniquePrefix(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	for text, want := range map[string]string{"lead": "lead-run-1", "planner-1": "planner-id", "PLANNER-1": "planner-id", "worker-id": "worker-id", "wor": "worker-id", "lead-run": "lead-run-1"} {
		if got, err := v.ResolveAgent(text); err != nil || got != want {
			t.Errorf("ResolveAgent(%q) = %q, %v; want %q", text, got, err, want)
		}
	}
	for _, text := range []string{"", "planner-9", "zzz", "p"} {
		_, err := v.ResolveAgent(text)
		if text == "p" {
			if err != nil {
				t.Errorf("p is the prefix of one id only, got %v", err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), "lead, planner-1, worker-1") {
			t.Errorf("ResolveAgent(%q) error = %v, want the list of agents", text, err)
		}
	}
}

// The plan number of a handoff is the one the agent gave. A planner usually gives none, which is
// zero on the wire, and zero is "not said": it must never read as plan 0.
func TestAHandoffNeverSaysPlanZero(t *testing.T) {
	for seed := range uint64(40) {
		r := rand.New(rand.NewPCG(seed, 8))
		said, owned := uint32(r.IntN(3)), uint32(r.IntN(3))
		v, agents := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
		planner := agents[1]
		planner.PlanNumber = owned
		v.Load(&pb.RunsSnapshot{Agents: []*pb.Agent{planner}})
		h := &pb.Message{Id: "h", RunId: "run-1", FromAgentId: planner.Id, ToAgentId: agents[0].Id, At: at(5),
			Body: &pb.Message_Handoff{Handoff: &pb.Handoff{PlanNumber: said, Claim: "plan 7 proposed"}}}
		changes := v.Apply(&pb.RunChange{Sequence: 2, RunId: "run-1", At: at(5), Body: &pb.RunChange_Message{Message: h}})
		if len(changes) != 1 {
			t.Fatalf("seed %d: %d changes", seed, len(changes))
		}
		line := v.Describe(changes[0], time.UTC)
		if strings.Contains(line, "plan 0") {
			t.Errorf("seed %d: said %d owned %d: %q", seed, said, owned, line)
		}
		want := said
		if said == 0 {
			want = owned
		}
		if (want != 0) != strings.Contains(line, fmt.Sprintf("handed off plan %d:", want)) {
			t.Errorf("seed %d: said %d owned %d: %q", seed, said, owned, line)
		}
	}
}

// --- text from agents is untrusted ---

var hostile = []string{
	"\x1b[2J\x1b[Hscreen cleared", "\x1b]0;title\x07set title", "\x1b]52;c;ZGF0YQ==\x1b\\clipboard", "plain \x1b[31mred\x1b[0m text",
	"\x1bPdevice\x1b\\control", "\x9b31mC1 csi", "line\rcarriage\rreturn", "bell\a backspace\b tab\t nul\x00 del\x7f", "bidi \u202e reversed \u2066 isolate",
	"bad utf8 \xff\xfe\xc0", "\x1b", "\x1b[", "\x1b]", "\x1b[31", "esc then newline\x1b\nnext", "zero\u200bwidth \ufeff bom", "emoji \U0001F600 and 日本語",
}

func unsafe(line string) bool {
	if !utf8.ValidString(line) {
		return true
	}
	for _, r := range line {
		if unicode.IsControl(r) || (r >= 0x202A && r <= 0x202E) || (r >= 0x2066 && r <= 0x2069) || r == 0xFEFF {
			return true
		}
	}
	return false
}

func TestSanitizeRemovesWhatCouldDriveATerminal(t *testing.T) {
	for _, text := range hostile {
		got := attachview.Sanitize(text)
		if strings.ContainsRune(strings.ReplaceAll(got, "\n", ""), 0) || unsafe(strings.ReplaceAll(got, "\n", " ")) {
			t.Errorf("Sanitize(%q) = %q still has a control", text, got)
		}
	}
	if got := attachview.Sanitize("a\x1b[31mb\x1b[0m\x1b]0;t\x07c"); got != "abc" {
		t.Errorf("escape sequences left %q, want abc", got)
	}
	if got := attachview.Sanitize("日本語 and emoji \U0001F600\n second"); got != "日本語 and emoji \U0001F600\n second" {
		t.Errorf("ordinary text was changed: %q", got)
	}
}

func FuzzSanitizeAndClip(f *testing.F) {
	for _, text := range hostile {
		f.Add(text, 10)
	}
	f.Add("ordinary text", 0)
	f.Add("", 3)
	f.Fuzz(func(t *testing.T, text string, width int) {
		clean := attachview.Sanitize(text)
		if unsafe(strings.ReplaceAll(clean, "\n", " ")) {
			t.Fatalf("Sanitize(%q) = %q has a control", text, clean)
		}
		if again := attachview.Sanitize(clean); again != clean {
			t.Fatalf("Sanitize is not idempotent: %q then %q", clean, again)
		}
		width = width % 300
		clipped := attachview.Clip(clean, width)
		if width > 0 && utf8.RuneCountInString(clipped) > width {
			t.Fatalf("Clip(%q, %d) = %q is %d runes", clean, width, clipped, utf8.RuneCountInString(clipped))
		}
		if width <= 0 && clipped != clean {
			t.Fatalf("Clip with width %d changed the text", width)
		}
		if utf8.RuneCountInString(clean) <= width && clipped != clean {
			t.Fatalf("Clip cut text that fits: %q at %d gave %q", clean, width, clipped)
		}
		if width > 3 && clipped != clean && !strings.HasSuffix(clipped, "...") {
			t.Fatalf("Clip cut without saying so: %q", clipped)
		}
		if !utf8.ValidString(clipped) {
			t.Fatalf("Clip made invalid UTF-8 of %q", clean)
		}
	})
}

// --- keys ---

func TestParseKeysReadsWhatTheTerminalSends(t *testing.T) {
	r := func(c rune) attachview.Key { return attachview.Key{Kind: attachview.KeyRune, Rune: c} }
	esc, enter, back, ctrlC := attachview.Key{Kind: attachview.KeyEsc}, attachview.Key{Kind: attachview.KeyEnter}, attachview.Key{Kind: attachview.KeyBackspace}, attachview.Key{Kind: attachview.KeyCtrlC}
	for in, want := range map[string][]attachview.Key{
		"a":          {r('a')},
		"a12\r":      {r('a'), r('1'), r('2'), enter},
		"\x1b":       {esc},
		"\x1b[A":     nil, // an arrow key is not Esc.
		"\x1b[1;5Cb": {r('b')},
		"\x1bOP":     nil,
		"\x1bx":      nil,
		"\x7f\x03":   {back, ctrlC},
		"é":          {r('é')},
		"\xff":       nil,
		"":           nil,
	} {
		if got := attachview.ParseKeys([]byte(in)); !slices.Equal(got, want) {
			t.Errorf("ParseKeys(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestParseKeysNeverPanicsOnAnyBytes(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 9))
	for range 2000 {
		b := make([]byte, r.IntN(12))
		for i := range b {
			b[i] = byte(r.IntN(256))
		}
		attachview.ParseKeys(b)
	}
}

// --- the properties of the whole view ---

// service is the in-memory stand-in for the two streams a view reads: what each agent has said, and
// what the view has been told so far.
type service struct {
	r      *rand.Rand
	store  map[string][]*pb.Item
	ids    int
	agents []string
}

var marker = regexp.MustCompile(`ITEM\[([a-z0-9-]+)#(\d+)\]`)

func (s *service) item(agent string, status pb.ItemStatus) *pb.Item {
	s.ids++
	ord := uint32(len(s.store[agent]) + 1)
	text := fmt.Sprintf("ITEM[%s#%d] %s", agent, ord, hostile[s.r.IntN(len(hostile))])
	it := &pb.Item{Id: fmt.Sprintf("%s-%d", agent, ord), TurnId: fmt.Sprintf("%s-turn-%d", agent, ord/3+1), AgentId: agent, Ordinal: ord, Status: status, At: at(int(ord))}
	switch s.r.IntN(5) {
	case 0:
		it.Body = &pb.Item_Reasoning{Reasoning: &pb.Text{Text: text}}
	case 1:
		it.Body = &pb.Item_Prompt{Prompt: &pb.Text{Text: text}}
	case 2:
		it.Body = &pb.Item_Tool{Tool: shell(text)}
	case 3:
		exit := int32(1)
		it.Body = &pb.Item_Tool{Tool: &pb.ToolCall{Call: &pb.ToolCall_Edit{Edit: &pb.EditCall{Targets: []string{text, "b"}}}, ExitCode: &exit, Output: hostile[s.r.IntN(len(hostile))]}}
	default:
		it.Body = &pb.Item_Assistant{Assistant: &pb.Text{Text: text}}
	}
	s.store[agent] = append(s.store[agent], it)
	return it
}

func (s *service) visible(it *pb.Item) bool {
	_, hidden := it.GetBody().(*pb.Item_Reasoning)
	return !hidden && it.GetStatus() != pb.ItemStatus_ITEM_STATUS_RUNNING
}

func (s *service) names() map[string]string {
	return map[string]string{"lead-run-1": "lead", "a1": "planner-1", "a2": "worker-1", "a3": "worker-2"}
}

// worldOfAgents is a lead with a small tree of children, to the view as its snapshot.
func (s *service) world() (*attachview.View, map[string]string) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)
	parent := map[string]string{}
	agents := []*pb.Agent{lead}
	s.agents = []string{lead.Id}
	for i, id := range []string{"a1", "a2", "a3"} {
		a := agentOf("run-1", id, []pb.AgentRole{pb.AgentRole_AGENT_ROLE_PLANNER, pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentRole_AGENT_ROLE_WORKER}[i], pb.AgentStatus_AGENT_STATUS_ACTIVE, i+1)
		a.ParentAgentId = s.agents[s.r.IntN(len(s.agents))]
		parent[id] = a.ParentAgentId
		agents = append(agents, a)
		s.agents = append(s.agents, id)
	}
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: agents, Sequence: 1})
	return v, parent
}

// Over any seeded sequence of keys, frames of the viewed agent, frames of other agents, and frames
// left over from views already left: every finished item of the viewed agent that the view was told of
// is printed exactly once and in the order it was told, nothing of another agent or of a view that was
// left is ever printed, and nothing that is printed can drive a terminal.
func TestTheViewPrintsEachItemOfTheViewedAgentOnceInOrderAcrossAnySwitches(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, 21))
		s := &service{r: r, store: map[string][]*pb.Item{}}
		v, _ := s.world()
		for _, id := range s.agents {
			for range r.IntN(8) {
				s.item(id, pb.ItemStatus_ITEM_STATUS_FINISHED)
			}
		}
		limit := 1 + r.IntN(6)
		m, outs := attachview.Begin(attachview.Options{Controls: true, History: limit, Width: 60 + r.IntN(60), Now: epoch, Zone: time.UTC}, v, nil)

		var (
			gen      uint64
			agent    string
			waiting  bool // a history is owed to the current view
			expected []string
			got      []string
			stale    []attachview.Input
			running  []*pb.Item
			finished = map[string]bool{} // the items of this view that were shown, by id
		)
		step := func(in attachview.Input) {
			var o []attachview.Output
			m, o = attachview.Update(m, in)
			outs = append(outs, o...)
		}
		absorb := func() {
			for _, o := range outs {
				switch o := o.(type) {
				case attachview.Print:
					for _, l := range o.Lines {
						if unsafe(l.Text) || unsafe(l.Label) {
							t.Fatalf("seed %d: a line holds a control: %+v", seed, l)
						}
						line := attachview.Render(l, 100)
						if unsafe(line) {
							t.Fatalf("seed %d: a line can drive a terminal: %q", seed, line)
						}
						for _, mk := range marker.FindAllStringSubmatch(line, -1) {
							got = append(got, mk[1]+"#"+mk[2])
						}
					}
				case attachview.Subscribe:
					if gen != 0 {
						stale = append(stale, attachview.AgentFrame{Gen: gen, Item: s.item(agent, pb.ItemStatus_ITEM_STATUS_FINISHED)})
						stale = append(stale, attachview.AgentHistory{Gen: gen, Items: s.store[agent]})
					}
					gen, agent, waiting = o.Gen, o.Agent, true
					got, expected, finished = nil, nil, map[string]bool{}
				case attachview.Exit:
					t.Fatalf("seed %d: the view ended on its own", seed)
				}
			}
			outs = nil
		}
		absorb()
		keys := []string{"a", "1", "2", "3", "4", "5", "\r", "\x1b", "b", "x", "\x7f"}
		for range 60 {
			switch r.IntN(8) {
			case 0, 1: // a key
				for _, k := range attachview.ParseKeys([]byte(keys[r.IntN(len(keys))])) {
					step(k)
				}
			case 2: // the history owed to the view
				if waiting {
					items := s.store[agent]
					if len(items) > limit {
						items = items[len(items)-limit:]
					}
					step(attachview.AgentHistory{Gen: gen, Items: items})
					waiting = false
					for _, it := range items {
						if s.visible(it) {
							expected = append(expected, it.AgentId+"#"+fmt.Sprint(it.Ordinal))
							finished[it.Id] = true
						}
					}
					for _, it := range s.store[agent] {
						if it.Status == pb.ItemStatus_ITEM_STATUS_RUNNING && slices.Contains(items, it) {
							running = append(running, it)
						}
					}
				}
			case 3, 4: // a new item of the viewed agent
				if !waiting {
					status := pb.ItemStatus_ITEM_STATUS_FINISHED
					if r.IntN(4) == 0 {
						status = pb.ItemStatus_ITEM_STATUS_RUNNING
					}
					it := s.item(agent, status)
					step(attachview.AgentFrame{Gen: gen, Item: it})
					if s.visible(it) {
						expected = append(expected, agent+"#"+fmt.Sprint(it.Ordinal))
						finished[it.Id] = true
					} else if status == pb.ItemStatus_ITEM_STATUS_RUNNING {
						running = append(running, it)
					}
					if r.IntN(3) == 0 { // the same item again: an update, not a second item.
						step(attachview.AgentFrame{Gen: gen, Item: it})
					}
				}
			case 5: // a running item finishes
				if !waiting && len(running) > 0 {
					i := r.IntN(len(running))
					it := running[i]
					if it.AgentId != agent {
						running = slices.Delete(running, i, i+1)
						break
					}
					done := proto.Clone(it).(*pb.Item)
					done.Status = pb.ItemStatus_ITEM_STATUS_FINISHED
					step(attachview.AgentFrame{Gen: gen, Item: done})
					if s.visible(done) && !finished[it.Id] {
						expected = append(expected, agent+"#"+fmt.Sprint(it.Ordinal))
						finished[it.Id] = true
					}
					running = slices.Delete(running, i, i+1)
				}
			case 6: // a frame of a view that was left, or of an agent that is not the viewed one
				if len(stale) > 0 {
					i := r.IntN(len(stale))
					step(stale[i])
					stale = slices.Delete(stale, i, i+1)
				}
				other := s.agents[r.IntN(len(s.agents))]
				if other != agent && !waiting {
					step(attachview.AgentFrame{Gen: gen, Item: s.item(other, pb.ItemStatus_ITEM_STATUS_FINISHED)})
				}
			default: // the terminal changes
				step(attachview.Resize{Width: 60 + r.IntN(60), Height: 24})
				step(attachview.Tick{Now: epoch.Add(time.Duration(r.IntN(10000)) * time.Second)})
			}
			absorb()
			if !slices.Equal(got, expected) {
				t.Fatalf("seed %d: viewing %s printed %v, want %v", seed, agent, got, expected)
			}
		}
	}
}

// Esc and b go up one agent at a time, to the agent that started the viewed one, and always end at the
// lead, whatever the shape of the tree and wherever the view starts.
func TestEscChainsAlwaysReachTheLead(t *testing.T) {
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, 33))
		run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
		lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)
		agents := []*pb.Agent{lead}
		ids := []string{lead.Id}
		for i := range 1 + r.IntN(8) {
			a := agentOf("run-1", fmt.Sprintf("agent-%d", i), pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ACTIVE, i+1)
			switch r.IntN(4) {
			case 0: // a parent the run does not have
				a.ParentAgentId = "gone"
			case 1: // an agent that is its own parent
				a.ParentAgentId = a.Id
			default:
				a.ParentAgentId = ids[r.IntN(len(ids))]
			}
			agents = append(agents, a)
			ids = append(ids, a.Id)
		}
		v := attachview.NewView("run-1")
		v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: agents, Sequence: 1})
		start := ids[r.IntN(len(ids))]
		m, _ := attachview.Begin(attachview.Options{Controls: true, Start: start, History: 10, Now: epoch, Zone: time.UTC}, v, nil)
		for press := 0; ; press++ {
			if m.Viewing() == lead.Id {
				break
			}
			if press > len(ids) {
				t.Fatalf("seed %d: still at %s after %d presses from %s", seed, m.Viewing(), press, start)
			}
			before := m.Viewing()
			key := "\x1b"
			if r.IntN(2) == 0 {
				key = "b"
			}
			var outs []attachview.Output
			for _, k := range attachview.ParseKeys([]byte(key)) {
				m, outs = attachview.Update(m, k)
			}
			var parent string
			for _, a := range agents {
				if a.Id == before {
					parent = a.ParentAgentId
				}
			}
			want := parent
			if parent == "" || parent == before || !slices.Contains(ids, parent) {
				want = lead.Id
			}
			if m.Viewing() != want {
				t.Fatalf("seed %d: from %s went to %s, want %s", seed, before, m.Viewing(), want)
			}
			if !slices.ContainsFunc(outs, func(o attachview.Output) bool { _, ok := o.(attachview.Subscribe); return ok }) {
				t.Fatalf("seed %d: going up from %s opened no view", seed, before)
			}
		}
		// the lead has nowhere above it.
		m, outs := attachview.Update(m, attachview.Key{Kind: attachview.KeyEsc})
		if m.Viewing() != lead.Id || len(outs) > 1 {
			t.Fatalf("seed %d: Esc at the lead moved to %s with %v", seed, m.Viewing(), outs)
		}
	}
}

// The view ends only through an Exit, after which it takes nothing and outputs nothing.
func TestNothingFollowsAnExit(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	for _, in := range []attachview.Input{attachview.Detach{}, attachview.StreamFailed{Err: fmt.Errorf("broken")}, attachview.Key{Kind: attachview.KeyCtrlC}} {
		m, _ := attachview.Begin(attachview.Options{Controls: true, History: 10, Now: epoch, Zone: time.UTC}, v, nil)
		m, outs := attachview.Update(m, in)
		if _, ok := outs[len(outs)-1].(attachview.Exit); !ok || !m.Done() {
			t.Fatalf("%T did not end the view: %v", in, outs)
		}
		if _, after := attachview.Update(m, attachview.Tick{Now: epoch}); len(after) != 0 {
			t.Errorf("%T: output after the exit: %v", in, after)
		}
	}
}

// A frame read under a subscription that was left is dropped, whatever it holds.
func TestAFrameOfAViewThatWasLeftIsDropped(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	m, outs := attachview.Begin(attachview.Options{Controls: true, History: 10, Now: epoch, Zone: time.UTC}, v, nil)
	first := subscribeOf(outs)
	m, _ = attachview.Update(m, attachview.AgentHistory{Gen: first.Gen})
	m, outs = attachview.Update(m, attachview.Key{Kind: attachview.KeyRune, Rune: 'a'})
	m, outs = attachview.Update(m, attachview.Key{Kind: attachview.KeyRune, Rune: '2'})
	second := subscribeOf(outs)
	if second.Agent != "planner-id" || second.Gen == first.Gen {
		t.Fatalf("switching opened %+v after %+v", second, first)
	}
	_, outs = attachview.Update(m, attachview.AgentFrame{Gen: first.Gen, Item: assistantItem("lead-run-1", 1, "t", "late")})
	if len(printed(outs)) != 0 {
		t.Errorf("a late frame printed %v", printed(outs))
	}
}
