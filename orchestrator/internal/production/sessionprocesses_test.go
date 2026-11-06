package production

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// fakeProcess stands for a session's process that holds the lock of a place: it takes the same
// kind of lock on the same file and writes a record beside it, and it dies by freeing the lock, as
// the system does for a process that ends however it ends. Nothing is started.
type fakeProcess struct {
	t     testing.TB
	place HolderPlace
	pid   int
	lock  *os.File
}

func newPlace(t testing.TB) HolderPlace {
	t.Helper()
	dir := t.TempDir()
	return HolderPlace{Socket: filepath.Join(dir, "sock"), Directory: dir}
}

// startFakeProcess is a process with pid that holds the place's lock and has recorded groups.
func startFakeProcess(t testing.TB, place HolderPlace, pid int, groups ...uint32) *fakeProcess {
	t.Helper()
	p := &fakeProcess{t: t, place: place, pid: pid}
	if err := os.MkdirAll(place.Directory, 0o700); err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(place.LockFile(), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("the lock was not free: %v", err)
	}
	p.lock = file
	writeRecord(t, place, &pb.SessionProcesses{HolderPid: uint32(pid), AgentProcessGroups: groupsOf(groups...)})
	t.Cleanup(p.die)
	return p
}

func (p *fakeProcess) holds() bool { return p.lock != nil }

func (p *fakeProcess) die() {
	if p.lock != nil {
		_ = p.lock.Close()
		p.lock = nil
	}
}

// groupsOf records groups with the start time their leaders had.
func groupsOf(ids ...uint32) []*pb.AgentProcessGroup {
	var out []*pb.AgentProcessGroup
	for _, id := range ids {
		out = append(out, &pb.AgentProcessGroup{Id: id, LeaderStarted: startedAt(int(id), 0)})
	}
	return out
}

// startedAt is the start time of the process with that pid in the boot-th run of the machine.
func startedAt(pid, boot int) string { return fmt.Sprintf("boot %d pid %d", boot, pid) }

func writeRecord(t testing.TB, place HolderPlace, record *pb.SessionProcesses) {
	t.Helper()
	data, err := proto.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(place.ProcessesFile(), data, 0o600); err != nil {
		t.Fatal(err)
	}
}

// recordingSignals is ProcessSignals that ends nothing. It notes what it was asked to end and
// whether the process held its lock at that moment, and a pid of the process it is told of ends
// that process, as a real SIGKILL would.
type recordingSignals struct {
	mu      sync.Mutex
	process *fakeProcess
	pids    []signalled
	pgids   []signalled
	// leaders is when the leader of each group started now; a group with no entry has no leader.
	leaders map[int]string
}

func (s *recordingSignals) LeaderStarted(pid int) (string, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	started, ok := s.leaders[pid]
	return started, ok, nil
}

// signalled is one signal, and whether the lock of the place was held when it was sent.
type signalled struct {
	id   int
	held bool
}

func (s *recordingSignals) Kill(pid int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pids = append(s.pids, signalled{pid, s.process != nil && s.process.holds()})
	if s.process != nil && s.process.pid == pid {
		s.process.die()
	}
	return nil
}

func (s *recordingSignals) KillGroup(pgid int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pgids = append(s.pgids, signalled{pgid, s.process != nil && s.process.holds()})
	return nil
}

func (s *recordingSignals) holders() []int { return ids(s.pids) }
func (s *recordingSignals) groups() []int  { return ids(s.pgids) }

func ids(list []signalled) []int {
	var out []int
	for _, n := range list {
		out = append(out, n.id)
	}
	return out
}

// The ways the lock of a place can be, and the ways its record can be.
const (
	lockNone = iota // no lock file: nothing ever held the place
	lockFree        // a lock file nothing holds: a holder that ended
	lockHeld        // a holder that lives
	locks
)

const (
	recordAbsent = iota
	recordValid
	recordGarbage   // not a message at all
	recordTruncated // the start of a valid message
	recordInvalid   // a message the contract refuses: no pid, or a group that is never one
	records
)

// The ways a recorded group can be when the host looks at it.
const (
	groupOurs     = iota // its leader runs and started when the record says
	groupNoLeader        // no leader: the group, if it exists, is still the agent's
	groupRecycled        // a leader runs that started at another time: the id went to another program
	groupUnknown         // the record has no start time, and a leader runs
	groupKinds
)

// scene is a place put into one of the combinations of the two.
type scene struct {
	place   HolderPlace
	lock    int
	record  int
	pid     int
	groups  []uint32
	kinds   []int // how each of groups is now
	process *fakeProcess
	signals *recordingSignals
}

func (s *scene) holders() *ProcessHolders { return &ProcessHolders{Signals: s.signals} }

func newScene(t testing.TB, r *rand.Rand, lock, record int) *scene {
	t.Helper()
	s := &scene{place: newPlace(t), lock: lock, record: record, signals: &recordingSignals{}}
	s.pid = 2 + r.IntN(1<<20)
	if r.IntN(2) == 0 {
		s.pid = os.Getpid() // a pid the system gave to a program that lives, such as this one.
	}
	for range r.IntN(4) {
		s.groups = append(s.groups, uint32(2+r.IntN(1<<20)))
		s.kinds = append(s.kinds, r.IntN(groupKinds))
	}
	s.signals.leaders = map[int]string{}
	recorded := make([]*pb.AgentProcessGroup, len(s.groups))
	for i, id := range s.groups {
		recorded[i] = &pb.AgentProcessGroup{Id: id, LeaderStarted: startedAt(int(id), 0)}
		switch s.kinds[i] {
		case groupOurs:
			s.signals.leaders[int(id)] = startedAt(int(id), 0)
		case groupRecycled: // after a reboot, say
			s.signals.leaders[int(id)] = startedAt(int(id), 1)
		case groupUnknown:
			recorded[i].LeaderStarted = ""
			s.signals.leaders[int(id)] = startedAt(int(id), 1)
		}
	}
	switch lock {
	case lockFree:
		if err := os.WriteFile(s.place.LockFile(), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	case lockHeld:
		s.process = startFakeProcess(t, s.place, s.pid)
		s.signals.process = s.process
	}
	valid := &pb.SessionProcesses{HolderPid: uint32(s.pid), AgentProcessGroups: recorded}
	switch record {
	case recordAbsent:
		if err := os.Remove(s.place.ProcessesFile()); err != nil && !errors.Is(err, os.ErrNotExist) {
			t.Fatal(err)
		}
	case recordValid:
		writeRecord(t, s.place, valid)
	case recordGarbage:
		data := append([]byte{0xff}, strconv.Itoa(r.IntN(1<<30))...) // wire type 7 does not exist
		writeBytes(t, s.place, data)
	case recordTruncated:
		data, _ := proto.Marshal(&pb.SessionProcesses{HolderPid: uint32(s.pid), AgentProcessGroups: groupsOf(1<<21, 1<<22)})
		writeBytes(t, s.place, data[:len(data)-1])
	case recordInvalid:
		bad := []*pb.SessionProcesses{{}, {HolderPid: uint32(s.pid), AgentProcessGroups: groupsOf(1)}, {AgentProcessGroups: groupsOf(5)}}
		writeRecord(t, s.place, bad[r.IntN(len(bad))])
	}
	return s
}

func writeBytes(t testing.TB, place HolderPlace, data []byte) {
	t.Helper()
	if err := os.WriteFile(place.ProcessesFile(), data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func (s *scene) recordExists() bool {
	_, err := os.Stat(s.place.ProcessesFile())
	return err == nil
}

// check holds what every operation keeps, whatever the lock and the record were: no pid is signalled
// unless the lock was held when it was, and no group unless it was free; a record that cannot be
// read ends nothing; and the outcome is a result or a named error.
func (s *scene) check(t *testing.T, op string, err error) {
	t.Helper()
	where := fmt.Sprintf("%s with lock %d, record %d", op, s.lock, s.record)
	for _, n := range s.signals.pids {
		if !n.held {
			t.Fatalf("%s: signalled pid %d though its lock was free", where, n.id)
		}
	}
	for _, n := range s.signals.pgids {
		if n.held {
			t.Fatalf("%s: ended group %d while its holder still held the lock", where, n.id)
		}
		if i := slices.Index(s.groups, uint32(n.id)); i < 0 || s.kinds[i] == groupRecycled || s.kinds[i] == groupUnknown {
			t.Fatalf("%s: ended group %d, whose leader started at another time than the record says", where, n.id)
		}
	}
	if s.record >= recordGarbage {
		if len(s.signals.pids)+len(s.signals.pgids) != 0 {
			t.Fatalf("%s: signalled %v and %v for a record that cannot be read", where, s.signals.pids, s.signals.pgids)
		}
	}
	var damaged *ProcessRecordError
	switch {
	case err == nil:
	case errors.Is(err, ErrHolderLives):
		if s.lock != lockHeld || op != "Spawn" {
			t.Fatalf("%s: refused a start though no holder lives", where)
		}
	case errors.As(err, &damaged):
		if damaged.Path != s.place.ProcessesFile() {
			t.Fatalf("%s: the error names %q, not the record", where, damaged.Path)
		}
	default:
		if s.lock != lockHeld || s.record != recordAbsent || op != "End" {
			t.Fatalf("%s: unnamed error %v", where, err)
		}
	}
}

func TestNoProcessIsSignalledWithoutTheLockItNeedsWhateverTheFilesSay(t *testing.T) {
	for seed := range uint64(200) {
		for lock := range locks {
			for record := range records {
				r := rand.New(rand.NewPCG(seed, uint64(lock*10+record)))
				for _, op := range []string{"End", "Reap", "Spawn"} {
					t.Run(fmt.Sprintf("%d/%d/%d/%s", seed, lock, record, op), func(t *testing.T) {
						s := newScene(t, r, lock, record)
						var err error
						switch op {
						case "End":
							err = s.holders().End(s.place)
						case "Reap":
							err = s.holders().Reap(s.place)
						case "Spawn":
							_, err = prepareStart(s.place, s.signals)
						}
						s.check(t, op, err)
						s.checkOutcome(t, op, err)
					})
				}
			}
		}
	}
}

// checkOutcome holds each combination to what it must do.
func (s *scene) checkOutcome(t *testing.T, op string, err error) {
	t.Helper()
	where := fmt.Sprintf("%s with lock %d, record %d", op, s.lock, s.record)
	pid := int(s.pid)
	wantGroups := func() []int {
		var out []int
		for i, g := range s.groups {
			if s.kinds[i] == groupOurs || s.kinds[i] == groupNoLeader {
				out = append(out, int(g))
			}
		}
		return out
	}
	switch {
	case s.lock == lockHeld && op == "End" && s.record == recordValid:
		if err != nil || !slices.Equal(s.signals.holders(), []int{pid}) || !slices.Equal(s.signals.groups(), wantGroups()) || s.recordExists() {
			t.Fatalf("%s: err %v, pids %v, groups %v, record kept %v; want the holder %d ended, then its groups %v, and the record gone", where, err, s.signals.holders(), s.signals.groups(), s.recordExists(), pid, wantGroups())
		}
	case s.lock == lockHeld && (op == "Reap" || op == "Spawn"):
		// A holder lives: its agents are its own, and its record is not touched.
		wantErr := op == "Spawn"
		if (err != nil) != wantErr || errors.Is(err, ErrHolderLives) != wantErr || len(s.signals.pids)+len(s.signals.pgids) != 0 || s.record != recordAbsent && !s.recordExists() {
			t.Fatalf("%s: err %v, signals %v %v, record kept %v", where, err, s.signals.pids, s.signals.pgids, s.recordExists())
		}
	case s.lock == lockHeld:
		// End of a holder whose record names nothing usable: it is named, and nothing is signalled.
		if err == nil {
			t.Fatalf("%s: no error for a holder it cannot name", where)
		}
	case s.record == recordValid:
		if err != nil || len(s.signals.pids) != 0 || !slices.Equal(s.signals.groups(), wantGroups()) || s.recordExists() {
			t.Fatalf("%s: err %v, pids %v, groups %v; want groups %v ended and the record gone", where, err, s.signals.pids, s.signals.groups(), wantGroups())
		}
	case s.record == recordAbsent:
		if err != nil || len(s.signals.pids)+len(s.signals.pgids) != 0 {
			t.Fatalf("%s: err %v, signals %v %v for nothing recorded", where, err, s.signals.pids, s.signals.pgids)
		}
	default:
		var damaged *ProcessRecordError
		if !errors.As(err, &damaged) || s.recordExists() {
			t.Fatalf("%s: err %v, record kept %v; want a named error and the record removed", where, err, s.recordExists())
		}
		// Reported once: the next look finds nothing to report.
		if _, again := reapUnheld(s.place, s.signals); again != nil {
			t.Fatalf("%s: the damaged record was reported again: %v", where, again)
		}
	}
}

// The lock decides whether a holder lives, whatever the record and the socket name say.
func TestAHolderLivesExactlyWhileItHoldsItsLock(t *testing.T) {
	place := newPlace(t)
	if alive(place) {
		t.Fatal("a place no holder ever started at has a holder")
	}
	p := startFakeProcess(t, place, 99, 100)
	if !alive(place) {
		t.Fatal("a holder with its lock is not alive")
	}
	p.die()
	if alive(place) {
		t.Fatal("a holder that ended is alive because its files remain")
	}
	// Asking does not take the place: a holder that starts after it can take its lock.
	again := startFakeProcess(t, place, 101)
	if !alive(place) || !again.holds() {
		t.Fatal("the host's look at the lock kept a new holder from taking it")
	}
}

// ReapAgent ends what every dead session of an agent left, and leaves the live one alone.
func TestReapingAnAgentEndsOnlyWhatNoHolderHolds(t *testing.T) {
	sidecars := &ProcessSidecars{Root: t.TempDir()}
	agentDirectory := sidecars.agentDir("agent-1")
	if err := os.MkdirAll(agentDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	signals := &recordingSignals{}
	var live *fakeProcess
	for n, groups := range map[string][]uint32{"1": {10}, "2": {20, 21}, "3": {30}} {
		dir := filepath.Join(agentDirectory, n)
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		place := HolderPlace{Socket: filepath.Join(dir, "sock"), Directory: dir}
		p := startFakeProcess(t, place, 500, groups...)
		if n == "3" {
			live = p
		} else {
			p.die()
		}
	}
	if err := os.WriteFile(filepath.Join(agentDirectory, "worktree"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := (&ProcessHolders{Signals: signals}).ReapAgent(sidecars.agentPlaces("agent-1")); err != nil {
		t.Fatal(err)
	}
	got := signals.groups()
	slices.Sort(got)
	if !slices.Equal(got, []int{10, 20, 21}) || len(signals.pids) != 0 || !live.holds() {
		t.Fatalf("ended groups %v and pids %v, want the dead sessions' groups and no pid", got, signals.pids)
	}
}

// A holder that was started refuses a second start at its place and keeps its record.
func TestAPlaceWithAHolderIsNotStartedAgain(t *testing.T) {
	place := newPlace(t)
	startFakeProcess(t, place, 77, 78)
	_, err := (&ProcessHolders{Signals: &recordingSignals{}}).Spawn(place, []string{"/nonexistent"}, place.Bootstrap())
	if !errors.Is(err, ErrHolderLives) {
		t.Fatalf("got %v, want ErrHolderLives", err)
	}
	if _, err := os.Stat(place.ProcessesFile()); err != nil {
		t.Fatalf("the live holder's record was touched: %v", err)
	}
}

// Whatever bytes a record holds, reading it gives a record the contract accepts or a named error.
func FuzzReadProcesses(f *testing.F) {
	valid, _ := proto.Marshal(&pb.SessionProcesses{HolderPid: 3, AgentProcessGroups: groupsOf(2, 9)})
	f.Add(valid)
	f.Add(valid[:2])
	f.Add([]byte{})
	f.Add([]byte{0xff, 0xff})
	f.Fuzz(func(t *testing.T, data []byte) {
		place := newPlace(t)
		writeBytes(t, place, data)
		record, err := readProcesses(place.ProcessesFile())
		var damaged *ProcessRecordError
		switch {
		case errors.As(err, &damaged):
			if record != nil {
				t.Fatal("a record that cannot be read was returned")
			}
		case err != nil:
			t.Fatalf("unnamed error %v", err)
		case record.GetHolderPid() == 0:
			t.Fatalf("accepted a record with no pid: %v", record)
		default:
			for _, g := range record.GetAgentProcessGroups() {
				if g.GetId() <= 1 {
					t.Fatalf("accepted group %d", g.GetId())
				}
			}
		}
	})
}

// A machine that rebooted since the record was written gave its group ids to other programs. The host
// ends none whose leader started at another time, and says so; it ends one whose leader is gone.
func TestAGroupIDAnotherProgramGotAfterARebootIsLeftAlone(t *testing.T) {
	place := newPlace(t)
	signals := &recordingSignals{leaders: map[int]string{
		11: startedAt(11, 1), // another program's now
		12: startedAt(12, 0), // still the agent
		// 13: no leader, and a group of that id the kernel still counts as the agent's.
		14: startedAt(14, 1), // the record has no start time for it
	}}
	writeRecord(t, place, &pb.SessionProcesses{HolderPid: 5, AgentProcessGroups: []*pb.AgentProcessGroup{
		{Id: 11, LeaderStarted: startedAt(11, 0)}, {Id: 12, LeaderStarted: startedAt(12, 0)},
		{Id: 13, LeaderStarted: startedAt(13, 0)}, {Id: 14},
	}})
	skipped, err := reapGone(place, signals)
	if err != nil {
		t.Fatal(err)
	}
	if got := signals.groups(); !slices.Equal(got, []int{12, 13}) {
		t.Fatalf("ended groups %v, want only 12 and 13", got)
	}
	if len(skipped) != 2 || !strings.Contains(skipped[0], "group 11") || !strings.Contains(skipped[1], "group 14") {
		t.Fatalf("said %v, want groups 11 and 14 named", skipped)
	}
}
