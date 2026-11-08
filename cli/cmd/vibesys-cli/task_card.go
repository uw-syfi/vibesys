package main

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

const taskUsage = "task: name a subcommand: card"

// taskCommand is `task card`. It asks the daemon, which is the only reader of task files, for the
// card a client shows of a task.
func taskCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("%s", taskUsage))
	}
	if args[0] == "card" {
		return taskCard(ctx, d, args[1:])
	}
	return fail(d, d.jsonErrors, usagef("task: %q is not a subcommand (card)", args[0]))
}

// taskCard is `task card <task> [--repo dir] [--revision rev | --working-tree | --folder dir]`:
//   - by default the task as committed at HEAD of the repository, which is what a run that starts
//     there would use;
//   - --revision reads another commit, branch or tag;
//   - --working-tree reads the files as they stand, committed or not, under --repo, which must be the
//     repository's root;
//   - --folder reads the task of a folder that is not a repository, such as an example's directory.
//
// A task with no card is an error that says why, naming the file and the key; with --json the
// card is printed as the contract's TaskCard.
func taskCard(ctx context.Context, d deps, args []string) int {
	fs, c := newFlags("task card", d)
	revision := fs.String("revision", "", "the commit, branch or tag to read the task at (default HEAD)")
	working := fs.Bool("working-tree", false, "read the files as they stand in the working tree, committed or not")
	folder := fs.String("folder", "", "read the task of this folder, which need not be a repository")
	positional, code, ok := planArgs(fs, args)
	if !ok {
		return code
	}
	name := c.task
	switch {
	case len(positional) == 1:
		name = positional[0]
	case len(positional) > 1:
		return fail(d, c.json, usagef("task card: give one task, got %q and %q", positional[0], positional[1]))
	}
	if strings.TrimSpace(name) == "" {
		return fail(d, c.json, usagef("task card: name the task, as an argument or with --task"))
	}
	req, err := describeRequest(d, name, c.repo, *revision, *working, *folder)
	if err != nil {
		return fail(d, c.json, err)
	}
	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, c.json, err)
	}
	defer cl.Close()
	resp, err := cl.DescribeTask(ctx, req)
	if err != nil {
		return fail(d, c.json, err)
	}
	if u := resp.GetUnreadable(); u != nil {
		if u.GetReason() == cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_UNSUPPORTED {
			return fail(d, c.json, &unavailable{message: u.GetDetail()})
		}
		// Not found and invalid are both a mistake in what was named or written: exit 2.
		return fail(d, c.json, usagef("%s", u.GetDetail()))
	}
	if resp.GetCard() == nil {
		return fail(d, c.json, &unavailable{message: "the daemon's answer is one this build cannot read"})
	}
	if c.json {
		printJSON(d, resp.GetCard())
		return exitOK
	}
	fmt.Fprint(d.stdout, describeCard(resp.GetCard()))
	return exitOK
}

// describeRequest is the request the flags make. The source is the one flag given; naming two is a
// usage error, since which one wins would be a guess.
func describeRequest(d deps, name, repo, revision string, working bool, folder string) (*cliv1.DescribeTaskRequest, error) {
	given := 0
	for _, g := range []bool{revision != "", working, folder != ""} {
		if g {
			given++
		}
	}
	if given > 1 {
		return nil, usagef("task card: --revision, --working-tree and --folder each name where to read the task: give at most one")
	}
	dir := func(p string) string {
		if p == "" {
			p = d.cwd
		} else if !filepath.IsAbs(p) {
			p = filepath.Join(d.cwd, p)
		}
		return filepath.Clean(p)
	}
	switch {
	case folder != "":
		return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Folder{Folder: &cliv1.FolderTask{Root: dir(folder), Task: name}}}, nil
	case working:
		return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_WorkingTree{WorkingTree: &cliv1.WorkingTreeTask{Repository: dir(repo), Task: name}}}, nil
	}
	committed := &cliv1.CommittedTask{Repository: dir(repo), Task: name}
	if revision != "" {
		committed.Revision = &revision
	}
	return &cliv1.DescribeTaskRequest{Source: &cliv1.DescribeTaskRequest_Committed{Committed: committed}}, nil
}

// describeCard is the card in words, one fact to a line with its name first and a tab after it, and
// a line for each objective.
func describeCard(card *cliv1.TaskCard) string {
	var b strings.Builder
	line := func(name, value string) {
		if value != "" {
			fmt.Fprintf(&b, "%s\t%s\n", name, value)
		}
	}
	line("task", card.GetTask())
	line("heading", card.GetHeading())
	if card.GetDomain() != cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED {
		// As a task writes it: lower case with a hyphen for each underscore.
		line("domain", strings.ReplaceAll(strings.ToLower(strings.TrimPrefix(card.GetDomain().String(), "TASK_DOMAIN_")), "_", "-"))
	}
	if h := card.GetHardware(); h != nil {
		line("hardware", fmt.Sprintf("%d x %s", h.GetCount(), card.GetHardwareName()))
	}
	if card.GetModelName() != "" {
		line("model", fmt.Sprintf("%s (%s)", card.GetModelName(), strings.ToLower(strings.TrimPrefix(card.GetModelMaker().String(), "MODEL_MAKER_"))))
	}
	if card.GetWorkloadName() != "" {
		line("workload", card.GetWorkloadName()+": "+card.GetWorkloadDescription())
	}
	for _, o := range card.GetObjectives() {
		direction := strings.ToLower(strings.TrimPrefix(o.GetDirection().String(), "TASK_OBJECTIVE_DIRECTION_"))
		fmt.Fprintf(&b, "objective\t%s\t%s\t%s\n", o.GetName(), direction, o.GetUnit())
	}
	return b.String()
}
