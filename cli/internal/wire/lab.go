package wire

import (
	"fmt"
	"maps"
	"slices"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// This file carries the calls on a lab and their replies (lab.proto) both ways: between the
// command line and the daemon, and between the daemon and the lab's supervisor.

var labRooms = []struct {
	domain core.LabRoom
	wire   cliv1.LabRoom
}{
	{core.LabCandidate, cliv1.LabRoom_LAB_ROOM_CANDIDATE},
	{core.LabTask, cliv1.LabRoom_LAB_ROOM_TASK},
}

// LabRoom returns the contract's value for a room.
func LabRoom(r core.LabRoom) cliv1.LabRoom {
	for _, p := range labRooms {
		if p.domain == r {
			return p.wire
		}
	}
	return cliv1.LabRoom_LAB_ROOM_UNSPECIFIED
}

// ToLabRoom returns the domain room for the contract's value, or invalid_input naming field.
func ToLabRoom(field string, r cliv1.LabRoom) (core.LabRoom, error) {
	for _, p := range labRooms {
		if p.wire == r {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a room of a lab", field, r)
}

var labSignals = []struct {
	domain core.LabSignal
	wire   cliv1.LabSignal
}{
	{core.LabTerm, cliv1.LabSignal_LAB_SIGNAL_TERM},
	{core.LabKill, cliv1.LabSignal_LAB_SIGNAL_KILL},
}

var labPhases = []struct {
	domain core.LabPhase
	wire   cliv1.LabPhase
}{
	{core.LabQueued, cliv1.LabPhase_LAB_PHASE_QUEUED},
	{core.LabStarting, cliv1.LabPhase_LAB_PHASE_STARTING},
	{core.LabReady, cliv1.LabPhase_LAB_PHASE_READY},
	{core.LabEnded, cliv1.LabPhase_LAB_PHASE_ENDED},
}

// LabCall returns the contract's message for a call on a lab.
func LabCall(c core.LabCall) *cliv1.LabCall {
	switch c := c.(type) {
	case core.LabExec:
		return &cliv1.LabCall{Op: &cliv1.LabCall_Exec{Exec: &cliv1.LabExec{
			Room: LabRoom(c.Room), Argv: slices.Clone(c.Argv), Env: maps.Clone(c.Env), Detach: c.Detach,
		}}}
	case core.LabStatusCall:
		return &cliv1.LabCall{Op: &cliv1.LabCall_Status{Status: &cliv1.LabStatusRequest{Wait: c.Wait}}}
	case core.LabKillCall:
		var signal cliv1.LabSignal
		for _, p := range labSignals {
			if p.domain == c.Signal {
				signal = p.wire
			}
		}
		return &cliv1.LabCall{Op: &cliv1.LabCall_Kill{Kill: &cliv1.LabKill{Handle: string(c.Handle), Signal: signal}}}
	case core.LabCheckoutCall:
		return &cliv1.LabCall{Op: &cliv1.LabCall_Checkout{Checkout: &cliv1.LabCheckout{Commit: string(c.Commit)}}}
	case core.LabPull:
		return &cliv1.LabCall{Op: &cliv1.LabCall_Pull{Pull: &cliv1.LabPull{Path: string(c.Path)}}}
	case core.LabPush:
		return &cliv1.LabCall{Op: &cliv1.LabCall_Push{Push: &cliv1.LabPush{
			Path: string(c.Path), Offset: c.Offset, Data: slices.Clone(c.Data), Executable: c.Executable, Last: c.Last,
		}}}
	case core.LabEndCall:
		return &cliv1.LabCall{Op: &cliv1.LabCall_End{End: &cliv1.LabEnd{}}}
	}
	return &cliv1.LabCall{}
}

// ToLabCall returns the domain call for the contract's message, validated: its rules (lab.proto),
// and a path that stays in the artifacts directory.
func ToLabCall(field string, c *cliv1.LabCall) (core.LabCall, error) {
	if c == nil {
		return nil, invalid("%s: missing", field)
	}
	if err := protovalidate.Validate(c); err != nil {
		return nil, invalid("%s: %v", field, err)
	}
	switch op := c.GetOp().(type) {
	case *cliv1.LabCall_Exec:
		room, err := ToLabRoom(field+".exec.room", op.Exec.GetRoom())
		if err != nil {
			return nil, err
		}
		argv := op.Exec.GetArgv()
		if argv[0] == "" {
			return nil, invalid("%s.exec.argv: a command needs a program to run", field)
		}
		var env map[string]string
		if len(op.Exec.GetEnv()) > 0 {
			env = maps.Clone(op.Exec.GetEnv())
		}
		return core.LabExec{Room: room, Argv: slices.Clone(argv), Env: env, Detach: op.Exec.GetDetach()}, nil
	case *cliv1.LabCall_Status:
		return core.LabStatusCall{Wait: op.Status.GetWait()}, nil
	case *cliv1.LabCall_Kill:
		for _, p := range labSignals {
			if p.wire == op.Kill.GetSignal() {
				return core.LabKillCall{Handle: core.LabHandle(op.Kill.GetHandle()), Signal: p.domain}, nil
			}
		}
		return nil, invalid("%s.kill.signal: %s is not a signal a lab sends", field, op.Kill.GetSignal())
	case *cliv1.LabCall_Checkout:
		return core.LabCheckoutCall{Commit: core.Commit(op.Checkout.GetCommit())}, nil
	case *cliv1.LabCall_Pull:
		p, err := toLabPath(field+".pull.path", op.Pull.GetPath())
		if err != nil {
			return nil, err
		}
		return core.LabPull{Path: p}, nil
	case *cliv1.LabCall_Push:
		p, err := toLabPath(field+".push.path", op.Push.GetPath())
		if err != nil {
			return nil, err
		}
		if p == "" {
			return nil, invalid("%s.push.path: a push names a file, not the artifacts directory", field)
		}
		return core.LabPush{
			Path: p, Offset: op.Push.GetOffset(), Data: slices.Clone(op.Push.GetData()),
			Executable: op.Push.GetExecutable(), Last: op.Push.GetLast(),
		}, nil
	case *cliv1.LabCall_End:
		return core.LabEndCall{}, nil
	}
	return nil, invalid("%s: no call", field)
}

// toLabPath reads a path that is already relative and clean, as the command line sends it: one
// that is not is refused, never cleaned into another.
func toLabPath(field, p string) (core.LabPath, error) {
	got, err := core.ParseLabPath(p, "")
	if err != nil || string(got) != p {
		return "", invalid("%s: %q is not a clean path relative to the artifacts directory", field, p)
	}
	return got, nil
}

// LabReply returns the contract's message for a reply of a lab call.
func LabReply(r core.LabReply) *cliv1.LabReply {
	switch r := r.(type) {
	case core.LabOutput:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Output{Output: slices.Clone(r.Data)}}
	case core.LabFile:
		return &cliv1.LabReply{Item: &cliv1.LabReply_File{File: &cliv1.LabFile{
			Path: r.Path, Offset: r.Offset, Data: slices.Clone(r.Data), Executable: r.Executable,
		}}}
	case core.LabExit:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Exit{Exit: labExit(r)}}
	case core.LabProcess:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Process{Process: labProcess(r)}}
	case core.LabStatus:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Status{Status: LabStatus(r)}}
	case core.LabPulled:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Pulled{Pulled: &cliv1.LabPulled{Files: uint32(r.Files), Bytes: r.Bytes}}}
	case core.LabPushed:
		return &cliv1.LabReply{Item: &cliv1.LabReply_Pushed{Pushed: &cliv1.LabPushed{Size: r.Size}}}
	}
	return &cliv1.LabReply{}
}

func labExit(e core.LabExit) *cliv1.LabExit {
	if e.Lost != "" {
		return &cliv1.LabExit{End: &cliv1.LabExit_Lost{Lost: e.Lost}}
	}
	return &cliv1.LabExit{End: &cliv1.LabExit_Code{Code: int32(e.Code)}}
}

func labProcess(p core.LabProcess) *cliv1.LabProcess {
	out := &cliv1.LabProcess{Handle: string(p.Handle), Room: LabRoom(p.Room), Argv: slices.Clone(p.Argv), StartedAt: timestamp(p.StartedAt)}
	switch {
	case p.End == nil:
		out.State = &cliv1.LabProcess_Running{Running: true}
	case p.End.Lost != "":
		out.State = &cliv1.LabProcess_Lost{Lost: p.End.Lost}
	default:
		out.State = &cliv1.LabProcess_ExitCode{ExitCode: int32(p.End.Code)}
	}
	return out
}

// LabStatus returns the contract's message for a lab's status.
func LabStatus(s core.LabStatus) *cliv1.LabStatus {
	out := &cliv1.LabStatus{Commit: string(s.Commit), Port: uint32(max(s.Port, 0)), Until: timestamp(s.Until), Artifacts: s.Artifacts}
	for _, p := range labPhases {
		if p.domain == s.Phase {
			out.Phase = p.wire
		}
	}
	for _, r := range s.Rooms {
		out.Rooms = append(out.Rooms, LabRoom(r))
	}
	for _, p := range s.Processes {
		out.Processes = append(out.Processes, labProcess(p))
	}
	return out
}

// LabServiceEnv is the variable, as NAME=value, that the task's container of a ready lab has to
// reach a server started in the candidate's container on the lab's port, and empty for a lab
// with no task's container: a task of one image.
func LabServiceEnv(s *cliv1.LabStatus) string {
	if s.GetPhase() != cliv1.LabPhase_LAB_PHASE_READY || !slices.Contains(s.GetRooms(), cliv1.LabRoom_LAB_ROOM_TASK) {
		return ""
	}
	return core.EnvServiceURL + "=" + core.ServiceURL(int(s.GetPort()))
}

// ToLabReply returns the domain reply for the contract's message, or invalid_input naming field
// for one that is empty or names a value this build does not know.
func ToLabReply(field string, r *cliv1.LabReply) (core.LabReply, error) {
	switch item := r.GetItem().(type) {
	case *cliv1.LabReply_Output:
		return core.LabOutput{Data: slices.Clone(item.Output)}, nil
	case *cliv1.LabReply_File:
		f := item.File
		return core.LabFile{Path: f.GetPath(), Offset: f.GetOffset(), Data: slices.Clone(f.GetData()), Executable: f.GetExecutable()}, nil
	case *cliv1.LabReply_Exit:
		return toLabExit(field+".exit", item.Exit)
	case *cliv1.LabReply_Process:
		return toLabProcess(field+".process", item.Process)
	case *cliv1.LabReply_Status:
		return ToLabStatus(field+".status", item.Status)
	case *cliv1.LabReply_Pulled:
		return core.LabPulled{Files: int(item.Pulled.GetFiles()), Bytes: item.Pulled.GetBytes()}, nil
	case *cliv1.LabReply_Pushed:
		return core.LabPushed{Size: item.Pushed.GetSize()}, nil
	}
	return nil, invalid("%s: an empty reply", field)
}

func toLabExit(field string, e *cliv1.LabExit) (core.LabExit, error) {
	switch end := e.GetEnd().(type) {
	case *cliv1.LabExit_Code:
		return core.LabExit{Code: int(end.Code)}, nil
	case *cliv1.LabExit_Lost:
		if end.Lost == "" {
			return core.LabExit{}, invalid("%s.lost: says nothing of why", field)
		}
		return core.LabExit{Lost: end.Lost}, nil
	}
	return core.LabExit{}, invalid("%s: no end", field)
}

func toLabProcess(field string, p *cliv1.LabProcess) (core.LabProcess, error) {
	h := core.LabHandle(p.GetHandle())
	if !h.Valid() {
		return core.LabProcess{}, invalid("%s.handle: %q is not a handle a lab gives", field, p.GetHandle())
	}
	room, err := ToLabRoom(field+".room", p.GetRoom())
	if err != nil {
		return core.LabProcess{}, err
	}
	out := core.LabProcess{Handle: h, Room: room, Argv: slices.Clone(p.GetArgv()), StartedAt: toTime(p.GetStartedAt())}
	switch state := p.GetState().(type) {
	case *cliv1.LabProcess_Running:
	case *cliv1.LabProcess_ExitCode:
		out.End = &core.LabExit{Code: int(state.ExitCode)}
	case *cliv1.LabProcess_Lost:
		if state.Lost == "" {
			return core.LabProcess{}, invalid("%s.lost: says nothing of why", field)
		}
		out.End = &core.LabExit{Lost: state.Lost}
	default:
		return core.LabProcess{}, invalid("%s: no state", field)
	}
	return out, nil
}

// ToLabStatus returns the domain status for the contract's message.
func ToLabStatus(field string, s *cliv1.LabStatus) (core.LabStatus, error) {
	out := core.LabStatus{Commit: core.Commit(s.GetCommit()), Port: int(s.GetPort()), Until: toTime(s.GetUntil()), Artifacts: s.GetArtifacts()}
	for _, p := range labPhases {
		if p.wire == s.GetPhase() {
			out.Phase = p.domain
		}
	}
	if out.Phase == "" {
		return core.LabStatus{}, invalid("%s.phase: %s is not a phase of a lab", field, s.GetPhase())
	}
	if out.Commit != "" && !out.Commit.Valid() {
		return core.LabStatus{}, invalid("%s.commit: %q is not a full commit id", field, s.GetCommit())
	}
	for i, r := range s.GetRooms() {
		room, err := ToLabRoom(fmt.Sprintf("%s.rooms[%d]", field, i), r)
		if err != nil {
			return core.LabStatus{}, err
		}
		out.Rooms = append(out.Rooms, room)
	}
	for i, p := range s.GetProcesses() {
		proc, err := toLabProcess(fmt.Sprintf("%s.processes[%d]", field, i), p)
		if err != nil {
			return core.LabStatus{}, err
		}
		out.Processes = append(out.Processes, proc)
	}
	return out, nil
}
