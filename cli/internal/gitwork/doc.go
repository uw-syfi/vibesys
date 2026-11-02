// Package gitwork gives a job the files of a commit through the git program: a clean checkout to
// run commands in, and a task as the commit has it.
//
// Both read the commit and never the working tree, so what a job runs is what was committed and
// not whatever is edited or untracked in the repository. Neither changes the repository: a
// checkout is a separate clone, and a task is read with git archive. Submodules are not
// checked out.
package gitwork
