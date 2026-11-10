package clustercli

import (
	"context"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/slurm"
)

// slurmTime is how squeue, sacct and scontrol write an instant: the cluster's local time, with no
// zone.
const slurmTime = "2006-01-02T15:04:05"

// clockLine is what a script's `date` prints for the login node's clock: the instant, and the same
// instant as the node's local time, from which a local time Slurm writes is made an instant.
const clockLine = `date '+%s %Y-%m-%dT%H:%M:%S'`

// loginClock converts the local times Slurm writes to instants, by the login node's own reading of
// its clock.
type loginClock struct {
	now    time.Time
	offset time.Duration // local time as if it were UTC, less the instant
}

// parseClock reads clockLine's output: the epoch seconds and the local time.
func parseClock(line string) (loginClock, bool) {
	f := strings.Fields(line)
	if len(f) != 2 {
		return loginClock{}, false
	}
	epoch, err := strconv.ParseInt(f[0], 10, 64)
	if err != nil {
		return loginClock{}, false
	}
	local, err := time.Parse(slurmTime, f[1])
	if err != nil {
		return loginClock{}, false
	}
	now := time.Unix(epoch, 0).UTC()
	return loginClock{now: now, offset: local.Sub(now)}, true
}

// at is the instant of a time Slurm wrote; false for "N/A", "Unknown", "None" and any text that is
// no time.
func (c loginClock) at(s string) (time.Time, bool) {
	t, err := time.Parse(slurmTime, strings.TrimSpace(s))
	if err != nil {
		return time.Time{}, false
	}
	return t.Add(-c.offset), true
}

// detailScript prints the login node's clock, squeue's state, reason, start, node, time left and
// time limit of job $1 and its exit status, sacct's state and end of it, and scontrol's record of
// it, which Slurm keeps for a few minutes after the job ends even where sacct has no database.
const detailScript = `printf 'clock '; ` + clockLine + `
q=$(squeue -h -j "$1" -o '%T|%r|%S|%N|%L|%l' 2>&1); r=$?
printf 'squeue %s\n%s\n==sacct==\n' "$r" "$q"
sacct -n -X -P -j "$1" -o State,End 2>/dev/null || true
printf '==scontrol==\n'
scontrol show job -o "$1" 2>/dev/null | tr ' ' '\n' | grep -E '^(JobState|EndTime)=' || true`

// Detail implements apptainer.Cluster.
func (c *Cluster) Detail(ctx context.Context, job string) (apptainer.JobDetail, error) {
	if strings.Trim(job, "0123456789") != "" || job == "" {
		return apptainer.JobDetail{}, invalid("%q is not a job id", job)
	}
	out, err := c.sh(ctx, CallTimeout, detailScript, job)
	if err != nil {
		return apptainer.JobDetail{}, fmt.Errorf("reading the state of the job %s: %w", job, err)
	}
	return ParseDetail(out)
}

// ParseDetail reads detailScript's output. A job squeue lists is as it says; one it does not list
// has ended, and sacct says how (then scontrol, then nothing: Slurm keeps no record of it). A job
// that squeue does not list and sacct says still lives, a line squeue wrote that is not what the
// script asks for, and a squeue that failed for any reason but an unknown job, are no answer: they
// fail as unavailable, and say nothing of the job.
func ParseDetail(out string) (apptainer.JobDetail, error) {
	head, rest, _ := strings.Cut(out, "==sacct==\n")
	sacct, scontrol, _ := strings.Cut(rest, "==scontrol==\n")
	lines := strings.SplitN(head, "\n", 3)
	if len(lines) < 2 {
		return apptainer.JobDetail{}, unavailable("the state of the job is %q, which is not what was asked for", clip(out))
	}
	clk, ok := parseClock(strings.TrimPrefix(lines[0], "clock "))
	if !ok {
		return apptainer.JobDetail{}, unavailable("the login node's clock reads %q", clip(lines[0]))
	}
	status, body := strings.TrimSpace(strings.TrimPrefix(lines[1], "squeue")), ""
	if len(lines) == 3 {
		body = strings.TrimSpace(strings.SplitN(lines[2], "\n", 2)[0])
	}
	if status != "0" && !strings.Contains(lines[len(lines)-1], "Invalid job id") {
		return apptainer.JobDetail{}, unavailable("squeue failed: %s", clip(strings.Join(lines[2:], "\n")))
	}
	if status == "0" && body != "" {
		return listed(body, sacct, clk)
	}
	return unlisted(sacct, scontrol, clk)
}

// listed reads the line squeue wrote for the job: state, reason, start, node, time left and limit.
// The reason may hold the separator, so the fields are counted from the ends.
func listed(line, sacct string, clk loginClock) (apptainer.JobDetail, error) {
	f := strings.Split(line, "|")
	if len(f) < 6 {
		return apptainer.JobDetail{}, unavailable("squeue's line for the job, %q, has %d of the 6 fields asked for", clip(line), len(f))
	}
	state := strings.TrimSpace(f[0])
	tail := f[len(f)-4:]
	d := apptainer.JobDetail{Phase: phaseOf(state), Name: state, Reason: strings.TrimSpace(strings.Join(f[1:len(f)-4], "|"))}
	start, startKnown := clk.at(tail[0])
	switch d.Phase {
	case apptainer.Queued:
		if startKnown {
			d.EstimatedStart = start
		}
	case apptainer.Running:
		left, okLeft := ParseDuration(tail[2])
		limit, okLimit := ParseDuration(tail[3])
		if !okLeft || !okLimit {
			return apptainer.JobDetail{}, unavailable("the job runs, and squeue says its time left is %q and its limit %q", tail[2], tail[3])
		}
		d.Node = strings.TrimSpace(tail[1])
		switch {
		case limit == maxDuration:
			// No limit: it has no end, and started when squeue says.
			d.StartedAt = start
		case left > limit:
			return apptainer.JobDetail{}, unavailable("the job has %v left of a limit of %v", left, limit)
		default:
			d.EndsAt = clk.now.Add(left)
			d.StartedAt = d.EndsAt.Add(-limit)
		}
	default:
		d.TimeLimit = strings.EqualFold(state, "TIMEOUT") || sacctSays(sacct, "TIMEOUT")
	}
	return d, nil
}

const maxDuration = time.Duration(1<<63 - 1)

// sacctSays says whether the state is the first state sacct wrote.
func sacctSays(sacct, state string) bool {
	name, _ := sacctState(sacct)
	return name == state
}

// sacctState is the first state sacct wrote, as its first word, and its end as the text sacct wrote.
func sacctState(sacct string) (state, end string) {
	for _, line := range strings.Split(sacct, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		s, e, _ := strings.Cut(line, "|")
		if f := strings.Fields(s); len(f) > 0 {
			// sacct writes "CANCELLED by 1002"; the first word is the state.
			return strings.ToUpper(strings.TrimRight(f[0], "+")), strings.TrimSpace(e)
		}
	}
	return "", ""
}

// unlisted reads what Slurm says of a job squeue does not list, which has ended unless the records
// say it lives.
func unlisted(sacct, scontrol string, clk loginClock) (apptainer.JobDetail, error) {
	state, end := sacctState(sacct)
	if state == "" {
		for _, line := range strings.Split(scontrol, "\n") {
			k, v, _ := strings.Cut(strings.TrimSpace(line), "=")
			switch k {
			case "JobState":
				state = strings.ToUpper(strings.TrimSpace(v))
			case "EndTime":
				end = strings.TrimSpace(v)
			}
		}
	}
	if state != "" && phaseOf(state) != apptainer.Ended {
		return apptainer.JobDetail{}, unavailable("squeue does not list the job, and the records say it is %s", state)
	}
	d := apptainer.JobDetail{Phase: apptainer.Ended, Name: state, TimeLimit: state == "TIMEOUT"}
	if at, ok := clk.at(end); ok {
		d.EndedAt = at
	}
	return d, nil
}

// CancelJob implements apptainer.Cluster: Slurm's cancel of the user's job, confirmed by squeue and
// then sacct, read every Poll for up to slurm.ConfirmTimeout.
func (c *Cluster) CancelJob(ctx context.Context, job string) error {
	if strings.Trim(job, "0123456789") != "" || job == "" {
		return invalid("%q is not a job id", job)
	}
	// scancel fails for a job that is not there, which is cancelled; whether it ended is read below.
	if _, err := c.sh(ctx, CallTimeout, `scancel -u `+user+` "$1" >/dev/null 2>&1; exit 0`, job); err != nil {
		return fmt.Errorf("cancelling the Slurm job %s: %w", job, err)
	}
	limit, stop := c.cfg.Timers.Timeout(ctx, slurm.ConfirmTimeout)
	defer stop()
	for {
		st, err := c.State(ctx, job)
		if err != nil {
			return fmt.Errorf("reading the state of the Slurm job %s: %w", job, err)
		}
		if st.Phase == apptainer.Ended {
			return nil
		}
		pause, stopPause := c.cfg.Timers.Timeout(ctx, c.cfg.Poll)
		select {
		case <-pause:
			stopPause()
		case <-limit:
			stopPause()
			return unavailable("the Slurm job %s was cancelled and still runs after %v", job, slurm.ConfirmTimeout)
		case <-ctx.Done():
			stopPause()
			return ctx.Err()
		}
	}
}

// partitionScript prints, each as a section made by sec (its exit status, then what it printed): the
// login node's clock, the user, the partition's node counts ("allocated/idle/other/total"), its
// nodes, the nodes Slurm shows as reserved, the pending jobs waiting for it, and the reservations,
// one a line.
const partitionScript = `sec() { n=$1; shift; o=$("$@" 2>&1); r=$?; printf '==vibesys:%s==\n%s\n%s\n' "$n" "$r" "$o"; }
sec clock ` + clockLine + `
sec user id -un
p="$1"
if [ -n "$p" ]; then
  sec sinfo sinfo -h -p "$p" -o %F
  sec nodes sinfo -h -p "$p" -N -o %N
  sec reserved sinfo -h -p "$p" -N -t reserved -o %N
  sec pending squeue -h -p "$p" -t PENDING -o %i
else
  sec sinfo sinfo -h -o %F
  sec nodes sinfo -h -N -o %N
  sec reserved sinfo -h -N -t reserved -o %N
  sec pending squeue -h -t PENDING -o %i
fi
sec res scontrol show reservation -o`

var partitionSections = []string{"clock", "user", "sinfo", "nodes", "reserved", "pending", "res"}

// Partition implements apptainer.Cluster.
func (c *Cluster) Partition(ctx context.Context, partition string) (apptainer.PartitionFacts, error) {
	if strings.ContainsAny(partition, " \t\n\x00") {
		return apptainer.PartitionFacts{}, invalid("%q is not a partition", partition)
	}
	out, err := c.sh(ctx, CallTimeout, partitionScript, partition)
	if err != nil {
		return apptainer.PartitionFacts{}, fmt.Errorf("reading the capacity of the partition %q: %w", partition, err)
	}
	return ParsePartition(out)
}

// ParsePartition reads partitionScript's output. A section whose command failed, but the
// reservations', is no answer (unavailable); a line it cannot read is skipped and named in Skipped,
// and a reservation scontrol does not list is none.
func ParsePartition(out string) (apptainer.PartitionFacts, error) {
	secs := sections(out, partitionSections)
	body := func(name string) (string, error) {
		s, ok := secs[name]
		if !ok {
			return "", unavailable("the answer has no section %q: %s", name, clip(out))
		}
		status, text, _ := strings.Cut(s, "\n")
		if strings.TrimSpace(status) != "0" && name != "res" {
			return "", unavailable("the %s of the partition could not be read: %s", name, clip(text))
		}
		return text, nil
	}
	var f apptainer.PartitionFacts
	text, err := body("clock")
	if err != nil {
		return f, err
	}
	clk, ok := parseClock(text)
	if !ok {
		return f, unavailable("the login node's clock reads %q", clip(text))
	}
	f.Now = clk.now
	if text, err = body("user"); err != nil {
		return f, err
	}
	f.User = strings.TrimSpace(text)
	if text, err = body("sinfo"); err != nil {
		return f, err
	}
	for _, line := range lines(text) {
		idle, total, ok := parseNodeCounts(line)
		if !ok {
			f.Skipped = append(f.Skipped, "sinfo: "+clip(line))
			continue
		}
		f.NodesIdle += idle
		f.NodesTotal += total
	}
	if text, err = body("nodes"); err != nil {
		return f, err
	}
	for _, n := range lines(text) {
		if !slices.Contains(f.Nodes, n) {
			f.Nodes = append(f.Nodes, n)
		}
	}
	// The nodes Slurm shows as reserved, which its idle count includes. An answer without the section,
	// or one that failed, names none, and a failed one is noted: the idle count is then Slurm's.
	if _, ok := secs["reserved"]; ok {
		if status, text, _ := strings.Cut(secs["reserved"], "\n"); strings.TrimSpace(status) != "0" {
			f.Skipped = append(f.Skipped, "sinfo -t reserved: "+clip(text))
		} else {
			for _, n := range lines(text) {
				if !slices.Contains(f.NodesReserved, n) {
					f.NodesReserved = append(f.NodesReserved, n)
				}
			}
		}
	}
	if text, err = body("pending"); err != nil {
		return f, err
	}
	f.PendingJobs = uint32(len(lines(text)))
	res, _ := body("res")
	if status, text, _ := strings.Cut(secs["res"], "\n"); strings.TrimSpace(status) != "0" && !strings.Contains(text, "No reservations") {
		f.Skipped = append(f.Skipped, "scontrol show reservation: "+clip(text))
		return f, nil
	}
	for _, line := range lines(res) {
		if strings.Contains(line, "No reservations") {
			continue
		}
		r, ok := parseReservation(line, clk)
		if !ok {
			f.Skipped = append(f.Skipped, "scontrol show reservation: "+clip(line))
			continue
		}
		f.Reservations = append(f.Reservations, r)
	}
	return f, nil
}

// lines are the non-empty lines of text, trimmed.
func lines(text string) []string {
	var out []string
	for _, l := range strings.Split(text, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out
}

// parseNodeCounts reads sinfo's %F, "allocated/idle/other/total".
func parseNodeCounts(line string) (idle, total uint32, ok bool) {
	f := strings.Split(line, "/")
	if len(f) != 4 {
		return 0, 0, false
	}
	var n [4]uint32
	for i, s := range f {
		v, err := strconv.ParseUint(strings.TrimSpace(s), 10, 32)
		if err != nil {
			return 0, 0, false
		}
		n[i] = uint32(v)
	}
	if n[1] > n[3] {
		return 0, 0, false
	}
	return n[1], n[3], true
}

// parseReservation reads one line of scontrol show reservation -o: Key=Value fields. A reservation
// needs a name and times it ends after it starts; without them it is not read.
func parseReservation(line string, clk loginClock) (apptainer.ReservationFacts, bool) {
	kv := map[string]string{}
	for _, field := range strings.Fields(line) {
		if k, v, ok := strings.Cut(field, "="); ok {
			kv[k] = v
		}
	}
	var r apptainer.ReservationFacts
	r.Name = kv["ReservationName"]
	start, okStart := clk.at(kv["StartTime"])
	end, okEnd := clk.at(kv["EndTime"])
	if r.Name == "" || !okStart || !okEnd || !end.After(start) {
		return r, false
	}
	r.Start, r.End = start, end
	if v, err := strconv.ParseUint(kv["NodeCnt"], 10, 32); err == nil {
		r.NodeCount = uint32(v)
	}
	r.Nodes, _ = ExpandHosts(kv["Nodes"])
	r.Partition = nullable(kv["PartitionName"])
	r.Users, r.Accounts, r.Groups = list(kv["Users"]), list(kv["Accounts"]), list(kv["Groups"])
	return r, true
}

func nullable(s string) string {
	if s == "(null)" {
		return ""
	}
	return s
}

func list(s string) []string {
	if s = nullable(s); s == "" {
		return nil
	}
	return strings.Split(s, ",")
}

// MaxHosts bounds the names ExpandHosts makes.
const MaxHosts = 100_000

// ExpandHosts expands a Slurm host list, "node[01-04,07],gpu3", into the names it stands for, with a
// range's width kept. It reports false, with the names made so far, for a list it cannot read or
// that stands for more than MaxHosts names; "(null)" and "" are none.
func ExpandHosts(list string) ([]string, bool) {
	list = strings.TrimSpace(list)
	if list == "" || list == "(null)" {
		return nil, true
	}
	var out []string
	for _, term := range splitTop(list) {
		var ok bool
		if out, ok = expandTerm(out, term); !ok {
			return out, false
		}
	}
	return out, true
}

// splitTop splits at the commas outside brackets.
func splitTop(s string) []string {
	var out []string
	depth, from := 0, 0
	for i, r := range s {
		switch r {
		case '[':
			depth++
		case ']':
			depth--
		case ',':
			if depth == 0 {
				out = append(out, s[from:i])
				from = i + 1
			}
		}
	}
	return append(out, s[from:])
}

// expandTerm appends the names of one term, which holds none or more bracket groups.
func expandTerm(out []string, term string) ([]string, bool) {
	open := strings.IndexByte(term, '[')
	if open < 0 {
		if term == "" || strings.ContainsAny(term, "]") {
			return out, false
		}
		return append(out, term), len(out) <= MaxHosts
	}
	closing := strings.IndexByte(term[open:], ']')
	if closing < 0 {
		return out, false
	}
	closing += open
	prefix, group, suffix := term[:open], term[open+1:closing], term[closing+1:]
	var names []string
	for _, rng := range strings.Split(group, ",") {
		lo, hi, isRange := strings.Cut(rng, "-")
		if !isRange {
			hi = lo
		}
		from, err1 := strconv.Atoi(lo)
		to, err2 := strconv.Atoi(hi)
		if err1 != nil || err2 != nil || from < 0 || to < from || to-from >= MaxHosts {
			return out, false
		}
		for n := from; n <= to; n++ {
			names = append(names, prefix+fmt.Sprintf("%0*d", len(lo), n))
			if len(names) > MaxHosts {
				return out, false
			}
		}
	}
	for _, name := range names {
		var ok bool
		if out, ok = expandTerm(out, name+suffix); !ok {
			return out, false
		}
	}
	return out, true
}

// reapScript kills every process of the node whose environment names the box $1, again each second
// for up to a minute, until none is left, and fails when some are.
const reapScript = `i=0
while :; do
  left=
  for f in $(grep -lxz -- "` + apptainer.EnvBox + `=$1" /proc/[0-9]*/environ 2>/dev/null); do
    p=${f#/proc/}
    kill -s KILL "${p%/environ}" 2>/dev/null && left=1
  done
  [ -z "$left" ] && exit 0
  i=$((i+1))
  if [ "$i" -ge 60 ]; then echo "processes of $1 are still there after a minute" >&2; exit 1; fi
  sleep 1
done`

// Reap implements apptainer.Node.
func (n *node) Reap(ctx context.Context, box string) error {
	if _, err := n.run(ctx, CallTimeout, reapScript, box); err != nil {
		return fmt.Errorf("killing %s on the node of the job %s: %w", box, n.job, err)
	}
	return nil
}
