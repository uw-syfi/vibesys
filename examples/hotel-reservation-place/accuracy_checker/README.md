Accuracy checker to verify the correctness of a running sever before VibeServe accepts it as a candidate.

Main Points Validated:

- **P1**: response schema and status code compatibility
- **P2**: a successful reservation shows up immediately when it is fetched
- **P3**: available room count drops after a booking (isolated and concurrent case)
- **P4**: two clients racing for the last room can't both win 
- **P5**: a failed reservation attempt leaves no partial trace in the system (atomicity)
- **P6**: held-out request sequences that are never used by the benchmark, so a server
  can't pass just by memorising the benchmark trace itself

'ALL' 6 points must pass successfully for a candidate to be accepted by the system. 


Start the server and run:

```bash
pip install httpx
python checker.py
```


System Status

Exits 0 if everything passes, 1 if anything fails.
