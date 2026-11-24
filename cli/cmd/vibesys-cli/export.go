package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// exportCommand is `export <dir> [--max-log-bytes n] [--outputs]`: a snapshot of a task's plans,
// jobs, logs, saved results, current result, bounds and workstreams, written into a directory with
// an index.json (ExportIndex) and a README.md. With a run (--run, or VIBESYS_RUN), only that run's
// jobs and plans are exported. It only reads: every file is the answer to a call another command
// makes, written as that command prints it, so the export says nothing the commands do not.
//
// The same state exports to the same bytes: file names come from plan numbers, job ids and commits,
// JSON is indented the one way, and nothing records when the export ran. Re-exporting into a
// directory kept in git therefore changes only what changed in the task.
func exportCommand(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("export", d)
	maxLog := fs.Uint64("max-log-bytes", 0, "keep at most this many bytes of each job's output, its head and its tail with a marker line between (default: keep all of it)")
	outputs := fs.Bool("outputs", false, "also copy the files each command job submitted with --outputs kept, as outputs --to does")
	positional, err := parse(fs, args)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	if len(positional) != 1 {
		return fail(d, c.json, usagef("export: give the directory to write into"))
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, c.json, err)
	}
	dir := positional[0]
	if !filepath.IsAbs(dir) {
		dir = filepath.Join(d.cwd, dir)
	}
	return withClient(ctx, d, c.json, func(cl *client.Client) int {
		x := &exporter{cl: cl, callCtx: callCtx, maxLog: *maxLog, outputs: *outputs, dir: dir}
		index, err := x.run(ctx)
		if err != nil {
			return fail(d, c.json, err)
		}
		if c.json {
			printJSON(d, index)
			return exitOK
		}
		fmt.Fprintf(d.stdout, "exported %s to %s\n", exportCounts(index), dir)
		return exitOK
	})
}

// exporter writes one export. Its calls are the ones the commands make: ListPlans and ViewPlan as
// plan list and plan view, ListJobs as jobs, ReadLogs as logs, Show as show, CurrentResult as
// result, ListBounds as bound list, ListWorkstreams as workstream list, and ReadOutputs as outputs.
type exporter struct {
	cl      *client.Client
	callCtx *cliv1.CallContext
	maxLog  uint64
	outputs bool
	dir     string
	root    *os.Root
	files   []*cliv1.ExportedFile
}

func (x *exporter) run(ctx context.Context) (*cliv1.ExportIndex, error) {
	if err := os.MkdirAll(x.dir, 0o755); err != nil {
		return nil, fmt.Errorf("making %s: %w", x.dir, err)
	}
	root, err := os.OpenRoot(x.dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	x.root = root

	jobs, err := x.cl.ListJobs(ctx, &cliv1.ListJobsRequest{Context: x.callCtx})
	if err != nil {
		return nil, err
	}
	jobs = x.ofRun(jobs)
	plans, err := x.cl.ListPlans(ctx, &cliv1.ListPlansRequest{Context: x.callCtx})
	if err != nil {
		return nil, err
	}
	steps := []func(context.Context) error{
		func(ctx context.Context) error { return x.plans(ctx, x.plansOfRun(plans.GetPlans(), jobs.GetJobs())) },
		func(context.Context) error { return x.jobs(jobs) },
		func(ctx context.Context) error { return x.logs(ctx, jobs.GetJobs()) },
		func(ctx context.Context) error { return x.copyOutputs(ctx, jobs.GetJobs()) },
		func(ctx context.Context) error { return x.results(ctx, jobs.GetJobs()) },
		x.taskWide,
	}
	for _, step := range steps {
		if err := step(ctx); err != nil {
			return nil, err
		}
	}
	index := &cliv1.ExportIndex{Task: x.callCtx.GetTask(), RunId: x.callCtx.GetRunId(), MaxLogBytes: x.maxLog, Outputs: x.outputs}
	if err := x.writeText("README.md", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_README, "", exportReadme(index)); err != nil {
		return nil, err
	}
	slices.SortFunc(x.files, func(a, b *cliv1.ExportedFile) int { return strings.Compare(a.GetPath(), b.GetPath()) })
	index.Files = x.files
	raw, err := exportJSON(index)
	if err != nil {
		return nil, err
	}
	return index, x.write("index.json", raw)
}

// ofRun is the jobs a caller of the run submitted, with why each queued one waits; every job when
// the export names no run.
func (x *exporter) ofRun(resp *cliv1.ListJobsResponse) *cliv1.ListJobsResponse {
	run := x.callCtx.GetRunId()
	if run == "" {
		return resp
	}
	out := &cliv1.ListJobsResponse{}
	kept := map[string]bool{}
	for _, j := range resp.GetJobs() {
		if slices.ContainsFunc(j.GetSubmitters(), func(s *cliv1.Submitter) bool { return s.GetRunId() == run }) {
			out.Jobs = append(out.Jobs, j)
			kept[j.GetId()] = true
		}
	}
	for _, w := range resp.GetWaits() {
		if kept[w.GetJobId()] {
			out.Waits = append(out.Waits, w)
		}
	}
	return out
}

// plansOfRun is the plans the run made, set a status of, or linked an exported job to; every plan
// when the export names no run.
func (x *exporter) plansOfRun(plans []*cliv1.Plan, jobs []*cliv1.Job) []*cliv1.Plan {
	run := x.callCtx.GetRunId()
	if run == "" {
		return plans
	}
	linked := map[uint32]bool{}
	for _, j := range jobs {
		for _, n := range j.GetPlans() {
			linked[n] = true
		}
	}
	var out []*cliv1.Plan
	for _, p := range plans {
		byRun := p.GetOrigin().GetRunId() == run ||
			slices.ContainsFunc(p.GetTransitions(), func(t *cliv1.PlanTransition) bool { return t.GetOrigin().GetRunId() == run })
		if byRun || linked[p.GetNumber()] {
			out = append(out, p)
		}
	}
	return out
}

// plans writes each plan's view, as JSON and as plan view prints it, and plans.txt as plan list
// prints them.
func (x *exporter) plans(ctx context.Context, plans []*cliv1.Plan) error {
	var list strings.Builder
	for _, p := range plans {
		fmt.Fprintln(&list, describePlan(p))
		resp, err := x.cl.ViewPlan(ctx, &cliv1.ViewPlanRequest{Context: x.callCtx, Number: p.GetNumber()})
		if err != nil {
			return err
		}
		name, subject := fmt.Sprintf("plan/%04d", p.GetNumber()), strconv.FormatUint(uint64(p.GetNumber()), 10)
		if err := x.writeMessage(name+".json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_PLAN_VIEW, subject, resp); err != nil {
			return err
		}
		if err := x.writeText(name+".txt", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_PLAN_VIEW_TEXT, subject, renderPlanView(resp.GetView())); err != nil {
			return err
		}
	}
	return x.writeText("plans.txt", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_PLAN_LIST_TEXT, "", list.String())
}

// jobs writes the job list, as JSON and as jobs prints it.
func (x *exporter) jobs(jobs *cliv1.ListJobsResponse) error {
	if err := x.writeMessage("jobs.json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_LIST, "", jobs); err != nil {
		return err
	}
	return x.writeText("jobs.txt", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_LIST_TEXT, "", renderJobs(jobs, x.callCtx.GetTask()))
}

// logs writes each job's output, whole or its head and tail.
func (x *exporter) logs(ctx context.Context, jobs []*cliv1.Job) error {
	for _, j := range jobs {
		name, err := jobFileName(j.GetId())
		if err != nil {
			return err
		}
		if err := x.log(ctx, j.GetId(), "log/"+name+".log"); err != nil {
			return err
		}
	}
	return nil
}

func (x *exporter) log(ctx context.Context, job, name string) error {
	keep := newHeadTail(x.maxLog)
	err := x.cl.Logs(ctx, &cliv1.ReadLogsRequest{Context: x.callCtx, JobId: job}, func(r *cliv1.ReadLogsResponse) error {
		keep.Write(r.GetData())
		return nil
	})
	if err != nil {
		return err
	}
	if err := x.write(name, keep.bytes()); err != nil {
		return err
	}
	x.files = append(x.files, &cliv1.ExportedFile{
		Path: name, Kind: cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_LOG, Subject: job,
		LogBytes: keep.total, OmittedBytes: keep.omitted(),
	})
	return nil
}

// copyOutputs copies, with --outputs, the files each command job kept.
func (x *exporter) copyOutputs(ctx context.Context, jobs []*cliv1.Job) error {
	if !x.outputs {
		return nil
	}
	for _, j := range jobs {
		if j.GetOutputs().GetKept() == nil {
			continue
		}
		name, err := jobFileName(j.GetId())
		if err != nil {
			return err
		}
		at := "outputs/" + name
		if _, err := x.cl.CopyOutputs(ctx, &cliv1.ReadOutputsRequest{Context: x.callCtx, JobId: j.GetId()}, filepath.Join(x.dir, filepath.FromSlash(at))); err != nil {
			return err
		}
		x.files = append(x.files, &cliv1.ExportedFile{Path: at, Kind: cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_OUTPUTS, Subject: j.GetId()})
	}
	return nil
}

// results writes what show says of every commit an exported job saved a result for.
func (x *exporter) results(ctx context.Context, jobs []*cliv1.Job) error {
	var commits []string
	for _, j := range jobs {
		if j.GetResultJobId() != "" && !slices.Contains(commits, j.GetSpec().GetCommit()) {
			commits = append(commits, j.GetSpec().GetCommit())
		}
	}
	slices.Sort(commits)
	for _, commit := range commits {
		resp, err := x.cl.Show(ctx, &cliv1.ShowRequest{Context: x.callCtx, Commit: commit})
		if err != nil {
			return err
		}
		if err := x.writeMessage("commit/"+commit+".json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_COMMIT_RESULTS, commit, resp); err != nil {
			return err
		}
		if err := x.writeText("commit/"+commit+".txt", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_COMMIT_RESULTS_TEXT, commit, renderShow(resp, commit)); err != nil {
			return err
		}
	}
	return nil
}

// taskWide writes what belongs to the task rather than to a run: the current result, the bounds and
// the workstreams.
func (x *exporter) taskWide(ctx context.Context) error {
	current, err := x.cl.CurrentResult(ctx, &cliv1.CurrentResultRequest{Context: x.callCtx})
	if err != nil {
		return err
	}
	if err := x.writeMessage("result.json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_CURRENT_RESULT, "", current); err != nil {
		return err
	}
	bounds, err := x.cl.ListBounds(ctx, &cliv1.ListBoundsRequest{Context: x.callCtx})
	if err != nil {
		return err
	}
	if err := x.writeMessage("bounds.json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_BOUNDS, "", bounds); err != nil {
		return err
	}
	workstreams, err := x.cl.ListWorkstreams(ctx, &cliv1.ListWorkstreamsRequest{Context: x.callCtx})
	if err != nil {
		return err
	}
	return x.writeMessage("workstreams.json", cliv1.ExportedFileKind_EXPORTED_FILE_KIND_WORKSTREAMS, "", workstreams)
}

func (x *exporter) writeMessage(name string, kind cliv1.ExportedFileKind, subject string, m proto.Message) error {
	raw, err := exportJSON(m)
	if err != nil {
		return err
	}
	if err := x.write(name, raw); err != nil {
		return err
	}
	x.files = append(x.files, &cliv1.ExportedFile{Path: name, Kind: kind, Subject: subject})
	return nil
}

func (x *exporter) writeText(name string, kind cliv1.ExportedFileKind, subject, text string) error {
	if err := x.write(name, []byte(text)); err != nil {
		return err
	}
	x.files = append(x.files, &cliv1.ExportedFile{Path: name, Kind: kind, Subject: subject})
	return nil
}

// write puts the bytes at the slash-separated name under the export's directory, replacing what is
// there. The file is written beside its place and renamed into it, so a file that is there is
// whole, and every path is opened through an os.Root of the directory, so nothing is written
// outside it.
func (x *exporter) write(name string, data []byte) error {
	at := filepath.FromSlash(name)
	if err := x.root.MkdirAll(filepath.Dir(at), 0o755); err != nil {
		return fmt.Errorf("export: %s: %w", name, err)
	}
	part := at + ".vibesys-part"
	if err := x.root.WriteFile(part, data, 0o644); err != nil {
		return fmt.Errorf("export: %s: %w", name, err)
	}
	if err := x.root.Rename(part, at); err != nil {
		_ = x.root.Remove(part)
		return fmt.Errorf("export: %s: %w", name, err)
	}
	return nil
}

// jobFileName is a job's id as a file name, which every id the daemon assigns is; an id that is not
// one is refused rather than written somewhere unexpected.
func jobFileName(id string) (string, error) {
	if id == "" || id == "." || id == ".." || path.Base(id) != id || strings.ContainsAny(id, `/\`) {
		return "", fmt.Errorf("export: the job id %q is not a file name", id)
	}
	return id, nil
}

// exportJSON is a message as --json prints it, but indented, one field per line, so a change to one
// field is a change to one line. protojson orders fields by number and map keys by value; its
// spacing varies from build to build on purpose, so it is compacted and indented again.
func exportJSON(m proto.Message) ([]byte, error) {
	raw, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("export: encoding %s: %w", m.ProtoReflect().Descriptor().FullName(), err)
	}
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return nil, fmt.Errorf("export: encoding %s: %w", m.ProtoReflect().Descriptor().FullName(), err)
	}
	out.WriteByte('\n')
	return out.Bytes(), nil
}

// headTail keeps a job's output: all of it when it is at most limit bytes long, or limit is 0, and
// otherwise its first half and its last half with a marker line between that says how much was left
// out. With a limit it holds at most limit bytes however long the output is.
type headTail struct {
	limit      uint64
	head, tail []byte
	// next is where in tail, a ring once it is full, the next byte goes.
	next  int
	total uint64
}

func newHeadTail(limit uint64) *headTail { return &headTail{limit: limit} }

var _ io.Writer = (*headTail)(nil)

func (h *headTail) Write(p []byte) (int, error) {
	n := len(p)
	h.total += uint64(n)
	if h.limit == 0 {
		h.head = append(h.head, p...)
		return n, nil
	}
	headCap, tailCap := int(h.limit/2), int(h.limit-h.limit/2)
	if room := headCap - len(h.head); room > 0 {
		take := min(room, len(p))
		h.head, p = append(h.head, p[:take]...), p[take:]
	}
	for len(p) > 0 {
		if len(h.tail) < tailCap {
			take := min(tailCap-len(h.tail), len(p))
			h.tail, p = append(h.tail, p[:take]...), p[take:]
			continue
		}
		if len(p) >= tailCap {
			copy(h.tail, p[len(p)-tailCap:])
			h.next, p = 0, nil
			continue
		}
		take := min(tailCap-h.next, len(p))
		copy(h.tail[h.next:], p[:take])
		h.next, p = (h.next+take)%tailCap, p[take:]
	}
	return n, nil
}

// omitted is how many bytes the kept output leaves out.
func (h *headTail) omitted() uint64 { return h.total - uint64(len(h.head)) - uint64(len(h.tail)) }

// bytes is the kept output, with the marker line where bytes were left out.
func (h *headTail) bytes() []byte {
	out := slices.Clone(h.head)
	if left := h.omitted(); left > 0 {
		out = fmt.Appendf(out, "\n[vibesys-cli export: %d of %d bytes left out here; the first %d and the last %d are kept]\n",
			left, h.total, len(h.head), len(h.tail))
	}
	out = append(out, h.tail[h.next:]...)
	return append(out, h.tail[:h.next]...)
}

// exportCounts is what an export wrote, counted, for its one line of text.
func exportCounts(index *cliv1.ExportIndex) string {
	count := map[cliv1.ExportedFileKind]uint64{}
	var cut uint64
	for _, f := range index.GetFiles() {
		count[f.GetKind()]++
		if f.GetOmittedBytes() > 0 {
			cut++
		}
	}
	parts := []string{
		counted(count[cliv1.ExportedFileKind_EXPORTED_FILE_KIND_PLAN_VIEW], "plan"),
		counted(count[cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_LOG], "job"),
		counted(count[cliv1.ExportedFileKind_EXPORTED_FILE_KIND_COMMIT_RESULTS], "measured commit"),
	}
	if cut > 0 {
		parts = append(parts, counted(cut, "log")+" cut to --max-log-bytes")
	}
	if index.GetOutputs() {
		parts = append(parts, counted(count[cliv1.ExportedFileKind_EXPORTED_FILE_KIND_JOB_OUTPUTS], "job")+"'s outputs")
	}
	return strings.Join(parts, ", ")
}

// exportReadme is the README.md of an export: what it is of, and where each part is.
func exportReadme(index *cliv1.ExportIndex) string {
	of := "every run"
	if index.GetRunId() != "" {
		of = "run `" + index.GetRunId() + "`"
	}
	logs := "each job's whole output"
	if index.GetMaxLogBytes() > 0 {
		logs = fmt.Sprintf("each job's output, cut to its head and tail when it is over %d bytes", index.GetMaxLogBytes())
	}
	return fmt.Sprintf(`# vibesys-cli export of task %s

The plans and jobs of %s, as `+"`vibesys-cli export`"+` read them from the daemon. JSON files are
the daemon's responses as `+"`--json`"+` prints them, indented; text files are what the command
prints without it. `+"`index.json`"+` (an ExportIndex, proto/vibesys/cli/v1/export.proto) lists
every file with what it holds.

| Path | What it holds | Command |
| --- | --- | --- |
| `+"`plans.txt`"+` | one line per plan | `+"`plan list`"+` |
| `+"`plan/NNNN.json`, `plan/NNNN.txt`"+` | a plan, its comments, statuses and linked jobs | `+"`plan view N`"+` |
| `+"`jobs.json`, `jobs.txt`"+` | every job, with its notes and linked plans | `+"`jobs`"+` |
| `+"`log/<job>.log`"+` | %s | `+"`logs <job>`"+` |
| `+"`outputs/<job>/`"+` | the files a command job kept, with `+"`--outputs`"+` | `+"`outputs <job> --to`"+` |
| `+"`commit/<sha>.json`, `commit/<sha>.txt`"+` | every result saved for a measured commit | `+"`show <sha>`"+` |
| `+"`result.json`"+` | the result branch's head and its number | `+"`result`"+` |
| `+"`bounds.json`"+` | every declared bound of the task | `+"`bound list`"+` |
| `+"`workstreams.json`"+` | every workstream of the task | `+"`workstream list`"+` |

The same state exports to the same bytes, so exporting again into this directory changes only what
changed.
`, index.GetTask(), of, logs)
}
