package attachview

import (
	"fmt"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The lines an item of an agent's conversation is shown as. They are the whole of how an item becomes
// text: the live view and the replay both come here. Every string that came from an agent or a tool
// passes Sanitize before it is part of a line.

// itemLines is the lines for a finished item, or none for one that is not shown (reasoning). Label
// names what a prompt was. Where a line is cut to the width is for Render to say.
func itemLines(it *pb.Item, label string) []Line {
	switch body := it.GetBody().(type) {
	case *pb.Item_Assistant:
		return assistantLines(body.Assistant.GetText())
	case *pb.Item_Prompt:
		return []Line{{Kind: LinePrompt, Label: label, Text: OneLine(body.Prompt.GetText(), 1<<20)}}
	case *pb.Item_Tool:
		return toolLines(it, body.Tool)
	}
	return nil // reasoning is hidden.
}

// assistantLines is an assistant's text in full, a line for each of its lines.
func assistantLines(text string) []Line {
	text = strings.TrimSpace(Sanitize(text))
	if text == "" {
		return nil
	}
	var lines []Line
	for i, line := range strings.Split(text, "\n") {
		kind := LineAssistantMore
		if i == 0 {
			kind = LineAssistant
		}
		lines = append(lines, Line{Kind: kind, Text: strings.TrimRight(line, " ")})
	}
	return lines
}

// toolLines is one line for the call, and a second for what went wrong when it failed.
func toolLines(it *pb.Item, t *pb.ToolCall) []Line {
	lines := []Line{{Kind: LineTool, Text: toolWords(t)}}
	if !toolFailed(it, t) {
		return lines
	}
	why := "failed"
	if t.ExitCode != nil {
		why = fmt.Sprintf("failed (exit %d)", t.GetExitCode())
	}
	if out := lastLine(t.GetOutput()); out != "" {
		why += ": " + out
	}
	return append(lines, Line{Kind: LineToolFailure, Text: why})
}

func toolFailed(it *pb.Item, t *pb.ToolCall) bool {
	return t.GetIsError() || t.GetExitCode() != 0 || it.GetStatus() == pb.ItemStatus_ITEM_STATUS_FAILED
}

// toolWords says what a call asked for in the fewest words, without its output.
func toolWords(t *pb.ToolCall) string {
	switch c := t.GetCall().(type) {
	case *pb.ToolCall_Shell:
		if cmd := firstLine(c.Shell.GetCommand()); cmd != "" {
			return "$ " + cmd
		}
		return "$ (command not yet known)"
	case *pb.ToolCall_Read:
		return "read " + OneLine(c.Read.GetTarget(), 1<<20)
	case *pb.ToolCall_Write:
		return "write " + OneLine(c.Write.GetTarget(), 1<<20)
	case *pb.ToolCall_Edit:
		targets := make([]string, 0, len(c.Edit.GetTargets()))
		for _, target := range c.Edit.GetTargets() {
			targets = append(targets, OneLine(target, 1<<20))
		}
		return "edit " + strings.Join(targets, ", ")
	case *pb.ToolCall_Search:
		return "search " + OneLine(c.Search.GetPattern(), 1<<20)
	case *pb.ToolCall_Other:
		return strings.TrimSpace(OneLine(c.Other.GetName(), 80) + " " + firstLine(c.Other.GetText()))
	}
	return "tool call"
}

// promptLabel says what started the turn a prompt belongs to, as the prompt's label.
func promptLabel(t pb.TurnTrigger) string {
	switch t {
	case pb.TurnTrigger_TURN_TRIGGER_BRIEF:
		return "brief"
	case pb.TurnTrigger_TURN_TRIGGER_OPERATOR:
		return "steer"
	case pb.TurnTrigger_TURN_TRIGGER_HANDOFF:
		return "handoff"
	case pb.TurnTrigger_TURN_TRIGGER_JOB_FINISHED:
		return "job finished"
	case pb.TurnTrigger_TURN_TRIGGER_MESSAGE:
		return "message"
	case pb.TurnTrigger_TURN_TRIGGER_REMINDER:
		return "reminder"
	}
	return "prompt"
}

// triggerWords says what started a turn: a steer, a worker that finished, a reminder.
func (v *View) triggerWords(t *pb.Turn) string {
	from := v.AgentName(v.Messages[t.GetTriggerMessageId()].GetFromAgentId())
	known := v.Messages[t.GetTriggerMessageId()].GetFromAgentId() != ""
	switch t.GetTrigger() {
	case pb.TurnTrigger_TURN_TRIGGER_BRIEF:
		return "its brief"
	case pb.TurnTrigger_TURN_TRIGGER_OPERATOR:
		return "your steer"
	case pb.TurnTrigger_TURN_TRIGGER_HANDOFF:
		if known {
			return from + " finished"
		}
		return "a handoff"
	case pb.TurnTrigger_TURN_TRIGGER_JOB_FINISHED:
		return "a job finished"
	case pb.TurnTrigger_TURN_TRIGGER_SCHEDULE:
		return "a schedule"
	case pb.TurnTrigger_TURN_TRIGGER_MESSAGE:
		if known {
			return "a message from " + from
		}
		return "a message"
	case pb.TurnTrigger_TURN_TRIGGER_SUBAGENT_SETTLED:
		return "a helper settled without a handoff"
	case pb.TurnTrigger_TURN_TRIGGER_REMINDER:
		return "a reminder"
	}
	return "an unknown trigger"
}
