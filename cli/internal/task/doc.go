// Package task reads a task from .vibesys/tasks/<name>/ and turns it into what a check or a
// measure job needs: the commands to run, how to read a benchmark's result, the metric and its
// direction, and a version that names exactly the task's files.
//
// It is pure. It reads through an fs.FS and never touches the machine, so a fuzzer can feed it any
// bytes. It rejects what it does not understand and what it cannot honor, with an error that
// names the key or the file, and never reads past it: a key it does not know is an error, since
// ignoring it could measure something other than what the task asks for.
package task
