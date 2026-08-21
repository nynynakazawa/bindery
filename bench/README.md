# Retrieval benchmark

```bash
python bench/run.py
```

Not a public benchmark and not comparable to one. The corpus is small and
hand-written; every note in it exists because some query should - or
specifically should not - return it. What the numbers are for is the
differences between columns, which is the only honest way to argue about a
change to ranking.

## Results

74 notes, 20 queries, `paraphrase-multilingual-MiniLM-L12-v2`, sqlite-vec:

| configuration | R@1 | R@5 | MRR | tok/q | p50 | leak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| keyword | 0.74 | 0.74 | 0.74 | 51 | 0.6ms | 0 |
| semantic | 0.68 | 0.74 | 0.71 | 148 | 15.9ms | 2 |
| hybrid | 0.79 | 0.84 | 0.82 | 148 | 15.7ms | 2 |
| **hybrid + project scope** | **0.79** | **0.84** | **0.82** | **120** | 13.4ms | **0** |

Each configuration earns its place, and the categories say why:

- **Keyword alone** is perfect on exact identifiers, exact error strings, and
  two-character Japanese, and scores **zero** on every paraphrase. It cannot
  match `how do we identify students` against a note about 大学メールアドレス.
- **Semantic alone** is the mirror image: it handles paraphrase and
  cross-language, and loses the identifier and short-CJK cases outright.
- **Fusing them** beats both on every aggregate. That is the whole argument
  for hybrid retrieval being the fixed default rather than a setting.
- **Project scope** costs nothing in accuracy and takes cross-project leakage
  from 2 to 0, while cutting tokens per query by a fifth.

## What the benchmark found

Both of these were bugs, not tuning:

- **Semantic search never returned nothing.** Cosine always has a best match,
  so a question the memory could not answer got a confident wrong one. A
  similarity floor took `tok/q` from 326 to 148 and R@1 from 0.74 to 0.79.
- **Result deduplication was suppressing correct answers.** Containment over
  a small shingle set is meaningless; one query was dropping two dozen
  passages as "duplicates".

## Known limitation

`短いCJK` sits at 0.50 because of one case: a query about something the vault
genuinely does not contain still returns a passage, because the embedding
model scores unrelated Japanese text just above the floor. Raising the floor
to fix it would drop four correct answers scoring between 0.313 and 0.344, so
the floor stays where it is. The real fix is a better multilingual model, not
a tuned threshold.
