package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// A CPU-only replay of a simulator-led campaign record (the optimize skill's simulator-led mode):
// from the record's calibration inputs, frozen predictions and measured outcomes, the steps the
// skills give an agent reach the decisions the record holds, with no device. The tool as shipped
// makes every decision it owns: the microbenchmark's timings come back through a command job's
// outputs, plan dependencies say what is ready, and compare and promote judge each integration by
// the task's declared samples, noise floor and min_gain. Only the roadmap's order rule, which the
// skill states and no tool holds, is applied here.
//
// The record in testdata is SYNTHETIC: no device, simulator or campaign produced its numbers. No
// archived record of a real campaign is available yet; a real one dropped in its place replays the
// same way.

const campaignRecord = "testdata/simulator-led/synthetic-campaign.json"

type campaign struct {
	Synthetic string `json:"synthetic"`
	Task      struct {
		Metric        string  `json:"metric"`
		Direction     string  `json:"direction"`
		Samples       int     `json:"samples"`
		RelativeNoise float64 `json:"relative_noise"`
		MinGain       float64 `json:"min_gain"`
	} `json:"task"`
	Head struct {
		Samples []float64 `json:"samples"`
	} `json:"head"`
	Plans        []campaignPlan `json:"plans"`
	Calibrations []struct {
		ID       string `json:"id"`
		Imported *struct {
			Plan      int `json:"plan"`
			DeviceJob struct {
				Question   string `json:"question"`
				Prediction string `json:"prediction"`
			} `json:"device_job"`
			File    string `json:"file"`
			Content string `json:"content"`
			SHA256  string `json:"sha256"`
		} `json:"imported_timings"`
	} `json:"calibrations"`
	Roadmap []struct {
		Version     int    `json:"version"`
		Calibration string `json:"calibration"`
		Order       []int  `json:"order"`
		Waiting     []int  `json:"waiting"`
		Parked      []int  `json:"parked"`
	} `json:"roadmap"`
	DeviceJobs []struct {
		Question   string `json:"question"`
		Prediction string `json:"prediction"`
		Decision   string `json:"decision"`
	} `json:"device_jobs"`
	Integrations []struct {
		Plan       int   `json:"plan"`
		Parts      []int `json:"parts"`
		Prediction struct {
			ComposedGain float64 `json:"composed_gain"`
		} `json:"prediction"`
		Samples  []float64 `json:"samples"`
		Decision struct {
			Verdict          string `json:"verdict"`
			DecidedByMinGain bool   `json:"decided_by_min_gain"`
			Promoted         bool   `json:"promoted"`
			Refusal          string `json:"refusal"`
		} `json:"decision"`
	} `json:"integrations"`
}

// campaignPlan is a roadmap item: its kind, what it builds on, and its frozen prediction (a relative
// gain of the headline) under each calibration.
type campaignPlan struct {
	Number    int                `json:"number"`
	Kind      string             `json:"kind"`
	Title     string             `json:"title"`
	DependsOn []int              `json:"depends_on"`
	Predicted map[string]float64 `json:"predicted"`
}

func loadCampaign(t *testing.T) campaign {
	t.Helper()
	data, err := os.ReadFile(campaignRecord)
	if err != nil {
		t.Fatal(err)
	}
	var c campaign
	// The record holds more than the replay reads (factors, residuals, dates), so fields it does not name are ignored.
	if err := json.Unmarshal(data, &c); err != nil {
		t.Fatalf("%s: %v", campaignRecord, err)
	}
	if !strings.HasPrefix(c.Synthetic, "SYNTHETIC") {
		t.Fatalf("%s must say it is synthetic until a real record replaces it", campaignRecord)
	}
	return c
}

// roadmapOrder is the skill's order rule: the plans whose prediction under the calibration exceeds
// the bar, largest first; a layout plan under the bar waits, any other is parked. Dependencies
// decide when a plan starts, not where it ranks.
func (c campaign) roadmapOrder(calibration string) (order, waiting, parked []int) {
	plans := slices.Clone(c.Plans)
	slices.SortStableFunc(plans, func(a, b campaignPlan) int {
		switch pa, pb := a.Predicted[calibration], b.Predicted[calibration]; {
		case pa > pb:
			return -1
		case pa < pb:
			return 1
		}
		return 0
	})
	for _, p := range plans {
		switch {
		case p.Predicted[calibration] > c.Task.MinGain:
			order = append(order, p.Number)
		case p.Kind == "layout":
			waiting = append(waiting, p.Number)
		default:
			parked = append(parked, p.Number)
		}
	}
	return order, waiting, parked
}

// replayTask is a task whose benchmark gives back, sample by sample, the values a commit's
// samples.txt records, and whose objectives are the record's.
func (m *machine) replayTask(c campaign) {
	m.t.Helper()
	m.git("init", "-q", "-b", "main")
	m.write("samples.txt", sampleLines(c.Head.Samples), 0o644)
	dir := task.TasksDir + "/bench/"
	// Each commit's samples.txt has its own counter, named by its checksum, so the n-th sample of a
	// commit is the n-th value it records.
	m.write(dir+task.ManifestName, fmt.Sprintf(`
version = "0.1"
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'test -s ${PROJECT_ROOT}/samples.txt']
timeout_seconds = 60

[benchmark]
command = ['sh', '-c', 'f=%s/$(cksum < ${PROJECT_ROOT}/samples.txt | cut -d" " -f1); n=$(cat "$f" 2>/dev/null | wc -c); printf x >> "$f"; v=$(sed -n "$((n+1))p" ${PROJECT_ROOT}/samples.txt); test -n "$v" && printf "{\"%s\": %%s}" "$v" > "$2"', 'sh']
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "%s"
`, m.counter, c.Task.Metric, c.Task.Metric), 0o644)
	m.write(dir+task.ObjectiveName, fmt.Sprintf("samples = %d\n\n[[objective]]\nname = %q\ndirection = %q\nrelative_noise = %v\nmin_gain = %v\n",
		c.Task.Samples, c.Task.Metric, c.Task.Direction, c.Task.RelativeNoise, c.Task.MinGain), 0o644)
	if err := os.MkdirAll(m.counter, 0o755); err != nil {
		m.t.Fatal(err)
	}
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "task")
}

func sampleLines(samples []float64) string {
	var b strings.Builder
	for _, s := range samples {
		b.WriteString(strconv.FormatFloat(s, 'f', -1, 64) + "\n")
	}
	return b.String()
}

// commitOn makes a commit on a new branch from base that writes the files, and returns it.
func (m *machine) commitOn(branch, base string, files map[string]string) string {
	m.t.Helper()
	m.git("checkout", "-q", "-b", branch, base)
	for path, content := range files {
		m.write(path, content, 0o644)
	}
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "feat: "+branch)
	return m.git("rev-parse", "HEAD")
}

func TestRealReplayOfASimulatorLedCampaignReachesTheRecordedDecisions(t *testing.T) {
	t.Parallel()
	c := loadCampaign(t)
	m := newMachine(t)
	m.replayTask(c)
	head := m.git("rev-parse", "HEAD")
	m.git("branch", "vibesys/result/bench", head)

	// The roadmap: the plans with their kinds and dependencies, in the store.
	for _, p := range c.Plans {
		args := []string{"--title", p.Title, "--kind", p.Kind}
		if len(p.DependsOn) > 0 {
			deps := make([]string, len(p.DependsOn))
			for i, d := range p.DependsOn {
				deps[i] = strconv.Itoa(d)
			}
			args = append(args, "--depends-on", strings.Join(deps, ","))
		}
		if got := m.createPlan(args...); got != strconv.Itoa(p.Number) {
			t.Fatalf("plan %q was created as %s, the record numbers it %d", p.Title, got, p.Number)
		}
	}

	// Each roadmap version is the order rule applied under its calibration, and a calibration
	// that changes the ranking makes a new version.
	for i, v := range c.Roadmap {
		order, waiting, parked := c.roadmapOrder(v.Calibration)
		if !slices.Equal(order, v.Order) || !slices.Equal(waiting, v.Waiting) || !slices.Equal(parked, v.Parked) {
			t.Errorf("roadmap v%d under %s: order %v, waiting %v, parked %v; the record has %v, %v, %v",
				v.Version, v.Calibration, order, waiting, parked, v.Order, v.Waiting, v.Parked)
		}
		if i > 0 && slices.Equal(v.Order, c.Roadmap[i-1].Order) {
			t.Errorf("roadmap v%d keeps v%d's order, so its calibration changed no ranking", v.Version, c.Roadmap[i-1].Version)
		}
	}

	// A device job that states no question, or no prediction, is refused.
	for i, j := range c.DeviceJobs {
		refused := j.Question == "" || j.Prediction == ""
		if refused != (j.Decision == "refused") {
			t.Errorf("device job %d (question %q, prediction %q) was %s", i, j.Question, j.Prediction, j.Decision)
		}
	}

	// The calibration's inputs: the microbenchmark's timings come back through the job's outputs,
	// byte for byte, as the record names them.
	for _, cal := range c.Calibrations {
		in := cal.Imported
		if in == nil {
			continue
		}
		kernel := m.commitOn(fmt.Sprintf("plan-%d", in.Plan), head, map[string]string{in.File: in.Content})
		note := fmt.Sprintf("Question: %s Predicts: %s.", in.DeviceJob.Question, in.DeviceJob.Prediction)
		r := m.cli("submit", "command", kernel, "--image", "harness", "--env", "host", "--outputs", "--wait",
			"--plan", strconv.Itoa(in.Plan), "--note", note, "--",
			"sh", "-c", `cp `+in.File+` "$VIBESYS_ARTIFACTS_DIR/"`)
		if r.code != exitOK {
			t.Fatalf("the microbenchmark = %+v", r)
		}
		job := strings.Fields(r.stdout)[0]
		to := filepath.Join(m.root, cal.ID)
		r = m.cli("outputs", job, "--to", to)
		if r.code != exitOK || !strings.Contains(r.stdout, "sha256:"+in.SHA256) {
			t.Fatalf("outputs of the microbenchmark = %+v, want %s with sha256 %s", r, in.File, in.SHA256)
		}
		if got, err := os.ReadFile(filepath.Join(to, in.File)); err != nil || string(got) != in.Content {
			t.Fatalf("the imported timings are %q, %v", got, err)
		}
		if r := m.cli("jobs", "--note-contains", "Predicts:"); !strings.Contains(r.stdout, job) {
			t.Errorf("jobs --note-contains Predicts: = %+v, want the microbenchmark %s", r, job)
		}
	}

	// The base, measured at the task's declared sample count.
	if r := m.cli("submit", "check", head, "--wait"); r.code != exitOK {
		t.Fatalf("the head's check = %+v", r)
	}
	m.measured(head, "--no-plan")
	won := map[int]bool{}

	// Each integration: one check, one measure at the task's samples, and the decision.
	for _, in := range c.Integrations {
		number := strconv.Itoa(in.Plan)
		deps := make([]string, len(in.Parts))
		for i, p := range in.Parts {
			deps[i] = strconv.Itoa(p)
		}
		if got := m.createPlan("--title", fmt.Sprintf("plans %s together gain %.1f%%", strings.Join(deps, ", "), 100*in.Prediction.ComposedGain),
			"--kind", "integration", "--depends-on", strings.Join(deps, ",")); got != number {
			t.Fatalf("integration plan created as %s, the record numbers it %d", got, in.Plan)
		}
		current := m.git("rev-parse", "vibesys/result/bench")
		candidate := m.commitOn("integrate-"+number, current, map[string]string{"samples.txt": sampleLines(in.Samples)})
		if ok, _ := m.check(candidate, number); !ok {
			t.Fatalf("integration %s fails its check", number)
		}
		m.measured(candidate, "--plan", number)

		cmp := m.cli("compare", candidate)
		verdict := strings.Fields(cmp.stdout + " ?")[0]
		if verdict != in.Decision.Verdict || strings.Contains(cmp.stdout, "decided by min_gain") != in.Decision.DecidedByMinGain {
			t.Errorf("compare of integration %s = %+v; the record has %s, decided by min_gain %v", number, cmp, in.Decision.Verdict, in.Decision.DecidedByMinGain)
		}
		promote := m.cli("promote", candidate)
		switch {
		case in.Decision.Promoted && promote.code != exitOK:
			t.Errorf("promote of integration %s = %+v; the record promoted it", number, promote)
		case !in.Decision.Promoted && (promote.code != exitFailed || !strings.Contains(promote.stdout, in.Decision.Refusal+":")):
			t.Errorf("promote of integration %s = %+v; the record refused it %s", number, promote, in.Decision.Refusal)
		}
		if !in.Decision.Promoted {
			continue
		}
		for _, p := range append(slices.Clone(in.Parts), in.Plan) {
			if r := m.cli("plan", "close", strconv.Itoa(p), "--status", "won", "--comment", "won in plan "+number+" ("+candidate+")"); r.code != exitOK {
				t.Fatalf("plan close %d = %+v", p, r)
			}
			won[p] = true
		}
		// A plan that waited on the parts it held is ready now: the layout waited on plan 1.
		ready := m.cli("plan", "list", "--ready")
		for _, p := range c.Plans {
			if !won[p.Number] && !slices.ContainsFunc(p.DependsOn, func(d int) bool { return !won[d] }) &&
				!strings.Contains(ready.stdout, fmt.Sprintf("%d\topen\t", p.Number)) {
				t.Errorf("plan list --ready after plan %s won = %+v, want plan %d", number, ready, p.Number)
			}
		}
	}

	// Every measured commit took exactly the task's declared samples.
	counters, err := os.ReadDir(m.counter)
	if err != nil {
		t.Fatal(err)
	}
	if len(counters) != 1+len(c.Integrations) {
		t.Errorf("%d commits were measured, want the head and %d integrations", len(counters), len(c.Integrations))
	}
	for _, f := range counters {
		if data, err := os.ReadFile(filepath.Join(m.counter, f.Name())); err != nil || len(data) != c.Task.Samples {
			t.Errorf("a commit took %d samples (%v), the task declares %d", len(data), err, c.Task.Samples)
		}
	}
}

// measured submits a measure at the task's declared sample count, linked as given, and waits for it.
func (m *machine) measured(commit string, link ...string) {
	m.t.Helper()
	r := m.cli(append([]string{"submit", "measure", commit, "--wait"}, link...)...)
	if r.code != exitOK {
		m.t.Fatalf("submit measure %s = %+v", commit, r)
	}
}
