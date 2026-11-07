# Accuracy checker: whisper-large-v3

`checker.py` judges the running server over HTTP. It sends each clip in
`../test_audio/` to `POST /v1/audio/transcriptions` with the form fields the
benchmark's Request Factory client sends (`file`, `model=whisper-large-v3`,
`response_format=json`, `temperature=0.0`) and compares the returned `text`
with the Hugging Face reference transcript of that clip in `references.json`.

Pass criterion, per clip: the transcript equals the reference after
normalization (lowercase, punctuation removed, whitespace collapsed), or the two
share at least 90% of their distinct words (`--threshold`, intersection over
union). Exit code 0 when every clip passes, 1 otherwise, including a failed
request.

```bash
# the server must already be running
python checker.py --url http://localhost:8000 --audio-dir ../test_audio
```

The gate is reference against candidate, not candidate against the ground truth
in `test_audio/manifest.json`: passing means reproducing the Hugging Face
implementation's transcripts, independent of whisper's own word error rate.

The checker imports nothing from the candidate, loads no model and uses only
Python's standard library. It reads its own directory and the clip directory,
which the task's accuracy command names. That is what the Modal run environment
copies into the serving container, where it runs the command next to the
server.

## Reference transcripts

`references.json` holds, per clip, the file name, the SHA-256 of the WAV file
and the reference transcript. The checker refuses to run when a clip's digest
differs. The transcripts come from `make_references.py`, which runs
`reference_transcribe` from `../reference/reference.py` (greedy,
`<|en|><|transcribe|><|notimestamps|>`), the reference this checker used to run
in-process, at the revision pinned in `../reference/meta.json`.

The checked-in file was made on CPU in float32 (torch 2.9.1, transformers
5.7.0; `meta` in the file). The same script on an NVIDIA H100 in float16, the
settings of the former in-process checker, produced identical transcripts.
Regenerate it only when the clips or the pinned revision change; the command is
in the script's docstring.
