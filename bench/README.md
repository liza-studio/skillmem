# Benchmarks

`longmemeval.py` scores retrieval against **LongMemEval** (Wu et al., ICLR 2025).

The oracle file is **not vendored** — it is a third-party dataset with its own
licence, and it is 15MB. Fetch it from the upstream project and drop it here as
`longmemeval_oracle.json`:

    https://github.com/xiaowu0162/LongMemEval

Then:

    python bench/longmemeval.py

## Reporting results

Report `recall@k` with the retrieval mode (BM25-only / vector-only / hybrid) and
the embedding model stated, so the number can be compared against other tools'
published methodology. A bare percentage without those two facts is unfalsifiable
and should not appear in the README.
