package connect

import (
	"errors"
	"os"
	"syscall"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Report is the host's end of the pipe its starter holds. Closing it, which the host's exit does too,
// ends what the starter reads, so a host says one thing, once. A host nobody started through a
// Launcher has a Report that says nothing.
type Report struct{ pipe *os.File }

// OpenReport takes the descriptor the starter named, or 0 for nobody listening.
func OpenReport(fd int) *Report {
	if fd == 0 {
		return &Report{}
	}
	// Nothing this host starts may hold the pipe open: its starter reads to the end.
	syscall.CloseOnExec(fd)
	return &Report{pipe: os.NewFile(uintptr(fd), "start report")}
}

// Serving says this host accepts connections on its socket.
func (r *Report) Serving() {
	r.say(&pb.HostStartReport{Outcome: &pb.HostStartReport_Serving{Serving: &pb.Serving{}}})
}

// AlreadyServed says another process holds the root.
func (r *Report) AlreadyServed() {
	r.say(&pb.HostStartReport{Outcome: &pb.HostStartReport_AlreadyServed{AlreadyServed: &pb.AlreadyServed{}}})
}

// Refuse says why this host will not serve, and is the error it ends with.
func (r *Report) Refuse(why *pb.Unserved) error {
	r.say(&pb.HostStartReport{Outcome: &pb.HostStartReport_Refused{Refused: why}})
	return errors.New(why.GetDetail())
}

// RefuseConfig is Refuse for a configuration that does not read.
func (r *Report) RefuseConfig(err error) error {
	return r.Refuse(&pb.Unserved{Reason: pb.UnservedReason_UNSERVED_REASON_CONFIG_REFUSED, Detail: err.Error()})
}

// Close ends the report. Said or not, the starter stops reading.
func (r *Report) Close() {
	if r.pipe != nil {
		_ = r.pipe.Close()
		r.pipe = nil
	}
}

func (r *Report) say(report *pb.HostStartReport) {
	if r.pipe == nil {
		return
	}
	if data, err := proto.Marshal(report); err == nil {
		_, _ = r.pipe.Write(data)
	}
	r.Close()
}
