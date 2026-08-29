"""
The /review compute : screen the hand-curated papers and pull the numbers out
of their text.

  build.py     orchestrator -- reads the sort board + the /images selection ,
               writes output/cache/review.json
  classify.py  inclusion screening , task categories , exclusion reasons
  extract.py   the field extractors ( architecture / acquisition / preprocessing )
  datasets.py  public-dataset detection + corpus-mined acquisition consensus

classify / extract / datasets are pure text -> value functions with no knowledge
of this project at all : hand one a string and it hands back values with the
verbatim spans they came from. build.py is the only file that knows where the
text lives. See build.py's docstring for the inclusion criteria and the output
shape.
"""
