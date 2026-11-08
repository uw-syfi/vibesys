package core

import "slices"

// Principal is a kind of caller: the operator, or an agent of a role.
type Principal string

// The principals. An agent's principal is its role.
const (
	PrincipalOperator Principal = "operator"
	PrincipalLead     Principal = Principal(RoleLead)
	PrincipalPlanner  Principal = Principal(RolePlanner)
	PrincipalWorker   Principal = Principal(RoleWorker)
	PrincipalJudge    Principal = Principal(RoleJudge)
	PrincipalCombiner Principal = Principal(RoleCombiner)
	PrincipalProfiler Principal = Principal(RoleProfiler)
)

// Action is what a caller can ask for, as a command or an agent tool.
type Action string

// The actions. ActionListAgents is a read and reaches no Decide; the tool server asks Allowed.
const (
	ActionStartRun    Action = "start_run"
	ActionStopRun     Action = "stop_run"
	ActionSpawnAgent  Action = "spawn_agent"
	ActionSendMessage Action = "send_message"
	ActionListAgents  Action = "list_agents"
	ActionStopAgent   Action = "stop_agent"
	ActionPostHandoff Action = "post_handoff"
	ActionPostReport  Action = "post_report"
	// ActionRequestCatchUp is the press of Catch me up, which is for the operator and no tool.
	ActionRequestCatchUp Action = "request_catch_up"
	// The operator's commands that change a live run or move it through setup.
	ActionUpdateRun     Action = "update_run"
	ActionBeginSetup    Action = "begin_setup"
	ActionCompleteSetup Action = "complete_setup"
	ActionDeclareTask   Action = "declare_task"
)

var subagents = []Principal{PrincipalPlanner, PrincipalWorker, PrincipalJudge, PrincipalCombiner, PrincipalProfiler}

// permissions is the one table of who may do what. Opening a tool to another role is one edit here.
var permissions = map[Action][]Principal{
	ActionStartRun:    {PrincipalOperator},
	ActionStopRun:     {PrincipalOperator},
	ActionSpawnAgent:  {PrincipalOperator, PrincipalLead},
	ActionSendMessage: {PrincipalOperator, PrincipalLead},
	ActionListAgents:  {PrincipalOperator, PrincipalLead},
	ActionStopAgent:   {PrincipalOperator, PrincipalLead},
	ActionPostHandoff: subagents,
	ActionPostReport:  {PrincipalLead},

	ActionRequestCatchUp: {PrincipalOperator},
	ActionUpdateRun:      {PrincipalOperator},
	ActionBeginSetup:     {PrincipalOperator},
	ActionDeclareTask:    {PrincipalLead},
	// ActionCompleteSetup has no entry until its behavior is decided: nobody is permitted, so a Decide
	// refuses it before it reaches the command switch. Whoever implements it adds its row here, for
	// the operator.
}

// Allowed says whether a principal may do an action.
func Allowed(p Principal, a Action) bool { return slices.Contains(permissions[a], p) }
