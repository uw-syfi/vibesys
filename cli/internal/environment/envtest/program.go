package envtest

// Op is one kind of step in a Program.
type Op string

// The steps a Program is made of. A step that waits is released by the test, and a step that
// signals is observed by the test, through the World the program runs in.
const (
	OpPrint      Op = "print"       // write Text and a newline to standard output
	OpWarn       Op = "warn"        // write Text and a newline to standard error
	OpDir        Op = "dir"         // write the working directory and a newline to standard output
	OpMark       Op = "mark"        // tell the test that the program reached the point Name
	OpHold       Op = "hold"        // wait until the test releases the point Name
	OpExit       Op = "exit"        // end the process with exit code Code
	OpCrash      Op = "crash"       // end the process as a process that was killed by SIGKILL
	OpChild      Op = "child"       // run Body in a new process of the same group, in the background
	OpAlive      Op = "alive"       // make this process observable: the test sees it end through Gone(Name)
	OpUnlessFlag Op = "unless-flag" // exit with code 1 unless the test has set the flag Name
)

// Step is one step of a Program. Which fields mean something depends on Op.
type Step struct {
	Op   Op
	Name string
	Text string
	Code int
	Body Program
}

// Program is what the suite asks a command to do, as data, so that an environment that runs shell
// commands, an environment that runs containers and a fake that interprets scripts are all asked
// the same thing. A World renders it for the environment that runs it.
//
// A process runs its steps in order. A Program that runs out of steps ends its process with exit
// code 0. A Child runs its body as a process of its own that shares the group of the process that
// started it: it outlives the process that started it unless the environment ends the group.
type Program []Step

// Print writes a line to standard output.
func Print(text string) Step { return Step{Op: OpPrint, Text: text} }

// Warn writes a line to standard error.
func Warn(text string) Step { return Step{Op: OpWarn, Text: text} }

// Dir writes the working directory to standard output.
func Dir() Step { return Step{Op: OpDir} }

// Mark tells the test that the program reached the point.
func Mark(point string) Step { return Step{Op: OpMark, Name: point} }

// Hold waits until the test releases the point.
func Hold(point string) Step { return Step{Op: OpHold, Name: point} }

// Exit ends the process with the code.
func Exit(code int) Step { return Step{Op: OpExit, Code: code} }

// Crash ends the process as if the kernel had killed it with SIGKILL: an environment reports it
// as exit code 137.
func Crash() Step { return Step{Op: OpCrash} }

// Child runs the body in the background, in the same process group.
func Child(body ...Step) Step { return Step{Op: OpChild, Body: body} }

// Alive makes the process that runs it observable through World.Gone(name). The test can ask
// whether the process still runs only after it has reached this step.
func Alive(name string) Step { return Step{Op: OpAlive, Name: name} }

// UnlessFlag ends the process with exit code 1 unless the test has set the flag.
func UnlessFlag(name string) Step { return Step{Op: OpUnlessFlag, Name: name} }

// Forever is a point no test ever releases: a Hold at it never returns.
const Forever = "forever"

// AliveMark is the point that Alive(name) marks once the process is observable. A World that
// implements Gone waits for it, so Gone is safe to call before the process has started.
func AliveMark(name string) string { return "alive-" + name }
