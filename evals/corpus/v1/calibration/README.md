# Judge calibration examples

These six hand-written, synthetic proposals are a regression tripwire for the
LLM judge. Two are excellent, two are mediocre in distinct ways, and two are
bad (a vague platitude; a leaky, wrong-shaped skill). Each JSON file declares
its expected band. The judge execution fails its calibration check when a bad
example scores at least as highly as an excellent example.
