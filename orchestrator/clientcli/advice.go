package clientcli

import (
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// runFailed is the error for a run that failed: why, in the lead's own words, and for a lead that
// never opened, what to do about it when its typed reason has a remedy. The advice follows the reason
// the orchestrator recorded and reads no text.
func (c *call) runFailed(v *View) error {
	text := "run " + v.RunID + " failed: " + orUnknown(v.failureDetail(v.Run))
	if advice := c.startAdvice(v.Lead().GetStartFailureReason()); advice != "" {
		text += "\n" + advice
	}
	return failed("%s", text)
}

// startAdvice says what to do about a session that never opened, for the reasons that have a
// remedy the operator can carry out, and is empty for the others.
func (c *call) startAdvice(reason pb.StartFailureReason) string {
	switch reason {
	case pb.StartFailureReason_START_FAILURE_REASON_AGENT_NOT_INSTALLED:
		return c.notInstalledAdvice()
	case pb.StartFailureReason_START_FAILURE_REASON_STATE_LOCATION_TOO_LONG:
		return "  to fix: a Unix socket address holds about 100 bytes, and the host keeps its sessions inside its state directory.\n" +
			"  Use a shorter root, such as `" + Program + " --root /var/tmp/vibesys run start ...` (a root other than the default keeps its state inside it),\n" +
			"  or a shorter state directory with `" + Program + " host configure --state-directory DIR`, then `" + Program + " host restart`."
	}
	return ""
}

// notInstalledAdvice names the directories the host searched for the agent program and how to change
// them. The host looks only where its configuration says, never in the caller's own PATH.
func (c *call) notInstalledAdvice() string {
	searched := "the host's default directories"
	if c.Configs != nil {
		if config, err := c.Configs.Load(c.root); err == nil {
			searched = strings.Join(hostconfig.ToolPath(config, c.Home), ", ")
		}
	}
	return "  to fix: the host looked for the agent program in " + searched + " and takes nothing from your shell's PATH.\n" +
		"  Install it in one of them, or name the directory that holds it with `" + Program + " host configure --tool-path DIR:DIR` (the list replaces this one),\n" +
		"  then `" + Program + " host restart`."
}
