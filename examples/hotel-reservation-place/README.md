Hotel Reservation Scenario - write-heavy system scenario

Important tasks done: initialisation of hotel system and room data, user set-up, concurrent reservation requests with overlapping date ranges,
interleaved availablilty reads and checking verification to avoid overbooking. 

Use:

* `--ref examples/hotel-reservation-place/reference`
* `--acc-checker examples/hotel-reservation-place/accuracy_checker`
* `--bench examples/hotel-reservation-place/benchmark`


To run locally, use the following commands:

```bash
# Install dependencies
pip install fastapi uvicorn httpx psutil
python reference/reference.py
python accuracy_checker/checker.py
python benchmark/benchmark.py --load-level medium
```

To run with VibeServe:

```bash
vibe-serve \
  --ref examples/hotel-reservation-place/reference \
  --acc-checker examples/hotel-reservation-place/accuracy_checker \
  --bench examples/hotel-reservation-place/benchmark \
  --exp-name hotel-place \
  --max-rounds 10
```