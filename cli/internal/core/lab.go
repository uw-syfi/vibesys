package core

import (
	"fmt"
	"path"
	"regexp"
	"strings"
	"time"
)

// A lab is a command job that holds the containers a run of its task has, with nothing running in
// them, for as long as its timeout, and runs in them whatever its caller asks: profiling and
// exploring, which are never scored. It saves no result, as no command job does, so nothing a lab
// runs can be compared, promoted or added to the frontier. It is scheduled as a command job: it
// holds a slot of its placement, and a measure, which holds its placement alone, never runs beside
// it.

// LabSpec says a command job is a lab. It has no settings of its own: the job's timeout is how long
// the lab lasts once its containers are up.
type LabSpec struct{}

// DefaultLabTime is how long a lab lasts when its submission says nothing.
const DefaultLabTime = 30 * time.Minute

// MaxLabTime is the longest a lab may ask to last.
const MaxLabTime = 24 * time.Hour

// LabRoom is a container of a lab. The set is closed.
type LabRoom string

// The rooms.
const (
	// LabCandidate is the candidate's image over the checkout, with the placement's GPUs; for a
	// task of one image, the task's image, which is the candidate's.
	LabCandidate LabRoom = "candidate"
	// LabTask is the task's image of a task of two images, in the candidate's network.
	LabTask LabRoom = "task"
)

// LabRooms returns every room, in the order the contract numbers them.
func LabRooms() []LabRoom { return []LabRoom{LabCandidate, LabTask} }

// LabSignal is what a process of a lab is sent to stop it. The set is closed.
type LabSignal string

// The signals.
const (
	LabTerm LabSignal = "term"
	LabKill LabSignal = "kill"
)

// LabPhase is where a lab is. The set is closed.
type LabPhase string

// The phases.
const (
	LabQueued   LabPhase = "queued"
	LabStarting LabPhase = "starting"
	LabReady    LabPhase = "ready"
	LabEnded    LabPhase = "ended"
)

// LabHandle names a process a lab started: p1, p2, ... in the order they started.
type LabHandle string

var labHandle = regexp.MustCompile(`^p[1-9][0-9]{0,8}$`)

// Valid reports whether the handle is one a lab gives.
func (h LabHandle) Valid() bool { return labHandle.MatchString(string(h)) }

// LabHandleOf is the handle of the n-th process a lab started, from 1.
func LabHandleOf(n int) LabHandle { return LabHandle(fmt.Sprintf("p%d", n)) }

// LabCall is one call on a running lab. The set is closed.
type LabCall interface{ labCall() }

// LabExec runs Argv in a room, as the invoking user, in the checkout, with Env over the room's
// variables. Detach starts it and answers with its process at once; otherwise its output is
// streamed and the answer is how it exited.
type LabExec struct {
	Room   LabRoom
	Argv   []string
	Env    map[string]string
	Detach bool
}

// LabStatusCall asks where the lab is and what runs in it: at once, or with Wait once it is ready
// or has ended.
type LabStatusCall struct{ Wait bool }

// LabKillCall signals a process the lab started, by its handle.
type LabKillCall struct {
	Handle LabHandle
	Signal LabSignal
}

// LabCheckoutCall replaces the lab's checkout with a clean checkout of Commit.
type LabCheckoutCall struct{ Commit Commit }

// LabPull copies what is at Path of the lab's artifacts directory back to the caller.
type LabPull struct{ Path LabPath }

// LabPush writes Data at Offset of the file at Path of the lab's artifacts directory. A piece at
// offset 0 starts the file afresh; Last makes the file visible to the lab's containers.
type LabPush struct {
	Path       LabPath
	Offset     uint64
	Data       []byte
	Executable bool
	Last       bool
}

// LabEndCall ends the lab now, as at the end of its time.
type LabEndCall struct{}

func (LabExec) labCall()         {}
func (LabStatusCall) labCall()   {}
func (LabKillCall) labCall()     {}
func (LabCheckoutCall) labCall() {}
func (LabPull) labCall()         {}
func (LabPush) labCall()         {}
func (LabEndCall) labCall()      {}

// LabReply is one message of a lab call's answer: output and file pieces as they come, then the
// answer. The set is closed.
type LabReply interface{ labReply() }

// LabOutput is output of a foreground exec.
type LabOutput struct{ Data []byte }

// LabFile is one piece of a file a pull copies back. Path is relative to the path pulled, and empty
// when that path is the file.
type LabFile struct {
	Path       string
	Offset     uint64
	Data       []byte
	Executable bool
}

// LabExit is how a foreground exec's command ended: its code, or, when Lost is set, nobody can say.
type LabExit struct {
	Code int
	Lost string
}

// LabProcess is a process the lab started for a detached exec, and where it is.
type LabProcess struct {
	Handle    LabHandle
	Room      LabRoom
	Argv      []string
	StartedAt time.Time
	// End is nil while it runs.
	End *LabExit
}

// LabStatus is where a lab is and what runs in it.
type LabStatus struct {
	Phase LabPhase
	// Commit is the commit the checkout is at now.
	Commit Commit
	// Port is a port of the candidate's container that was free when the lab started, for a
	// server the caller starts; the task's container reaches it at 127.0.0.1.
	Port int
	// Until is when the lab ends on its own; zero until it is ready.
	Until     time.Time
	Rooms     []LabRoom
	Processes []LabProcess
	// Artifacts is where the containers see the artifacts directory.
	Artifacts string
}

// LabPulled ends a pull.
type LabPulled struct {
	Files int
	Bytes uint64
}

// LabPushed answers a push with the file's size so far.
type LabPushed struct{ Size uint64 }

func (LabOutput) labReply()  {}
func (LabFile) labReply()    {}
func (LabExit) labReply()    {}
func (LabProcess) labReply() {}
func (LabStatus) labReply()  {}
func (LabPulled) labReply()  {}
func (LabPushed) labReply()  {}

// LabPath is a path in a lab's artifacts directory: relative, clean, and never above it. The empty
// path is the directory itself.
type LabPath string

// ParseLabPath reads a path the caller gave for the lab's artifacts directory: relative to it, or
// absolute under where the containers see it (artifacts). A path that leaves the directory, or
// names it through a parent, is refused with FailureInvalidInput naming it.
func ParseLabPath(p, artifacts string) (LabPath, error) {
	refuse := func(why string) (LabPath, error) {
		return "", failf(FailureInvalidInput, "path %q: %s; a lab copies files in and out of its artifacts directory (%s) only, so copy others there with lab exec", p, why, artifacts)
	}
	if strings.ContainsAny(p, "\x00") {
		return refuse("it holds a NUL")
	}
	rel := p
	if path.IsAbs(p) {
		root := path.Clean(artifacts)
		clean := path.Clean(p)
		switch {
		case clean == root:
			return "", nil
		case strings.HasPrefix(clean, root+"/"):
			rel = strings.TrimPrefix(clean, root+"/")
		default:
			return refuse("it is outside the artifacts directory")
		}
	}
	for _, part := range strings.Split(rel, "/") {
		if part == ".." {
			return refuse("it climbs out with ..")
		}
	}
	clean := path.Clean(rel)
	if clean == "." {
		return "", nil
	}
	return LabPath(clean), nil
}

// Join is the path of the lab's artifacts directory dir.
func (p LabPath) Join(dir string) string {
	if p == "" {
		return dir
	}
	return path.Join(dir, string(p))
}
