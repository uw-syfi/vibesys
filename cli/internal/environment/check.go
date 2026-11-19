package environment

import (
	"fmt"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Check runs a check that what a job is given may be used (resource.Attachment.Check, or
// resource.Devices.Check) before any of the job's commands, through the launcher, within the
// limit, and says how the run ends when it fails: nothing of the execution ran, so it is
// core.NotStarted with a core.FailureUnavailable, and its message is what failed makes of what the
// check printed. It returns nil when the check passed or argv is empty, and an ExecCancelled end
// when the caller cancels while it runs. Only ev.Limit and ev.Cancelled are read. It ends the
// check's process on every path but a pass, where the process has ended by itself.
func Check(l Launcher, argv []string, ev Events, failed func(why string) string) core.Attempt {
	if len(argv) == 0 {
		return nil
	}
	out := &Tail{}
	proc, err := l.Start(argv, out)
	if err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	refuse := func(why string) core.Attempt {
		return core.NotStarted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: failed(why)}}
	}
	select {
	case st := <-proc.Exited():
		switch {
		case st.Err != nil:
			return refuse(fmt.Sprintf("its check did not run to its end (%v)", st.Err))
		case st.Code != 0:
			said := strings.TrimSpace(out.String())
			if said == "" {
				said = fmt.Sprintf("its check exited with %d and said nothing", st.Code)
			}
			return refuse(said)
		}
		return nil
	case <-ev.Limit:
		proc.Kill()
		<-proc.Exited()
		return refuse("its check did not end within the command's limit")
	case <-ev.Cancelled:
		proc.Kill()
		<-proc.Exited()
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
}
