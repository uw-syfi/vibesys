package clientcli

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
)

// The run as a client sees it, and the words that tell it, belong to attachview, where `run attach` is
// made, so that a change becomes a line in one place. The verbs here use them under their own names.

// View is one run as a client sees it. See attachview.View.
type View = attachview.View

// Change is one thing that happened to a run. See attachview.Change.
type Change = attachview.Change

// NewView is the empty view of a run.
func NewView(run string) *View { return attachview.NewView(run) }

var (
	statusWord  = attachview.StatusWord
	clock       = attachview.Clock
	oneLine     = attachview.OneLine
	agentLabel  = attachview.AgentLabel
	endDetail   = attachview.EndDetail
	stopReason  = attachview.StopReason
	limitWord   = attachview.LimitWord
	reportWord  = attachview.ReportWord
	withPrefix  = attachview.WithPrefix
	usageWords  = attachview.UsageWords
	shortCommit = attachview.ShortCommit
	jobKind     = attachview.JobKind
)

// live says a run has not reached a final status.
func live(r *pb.Run) bool { return attachview.Live(r) }
