<!-- codegraph -->
## Code questions: query the graph first

This repo has a CodeGraph index in `.codegraph/` (tree-sitter, local, no API key).

For any "how does X work / where is X / what breaks if I change X" question, call
`codegraph_explore` **first** — one call returns the verbatim source of the relevant
symbols plus their callers/callees:

```
mcp__codegraph__codegraph_explore(query="<symbol names>", projectPath="<repo path>")
# shell equivalent:  codegraph explore "handle_voice transcribe_with_groq"
```

**Query with SYMBOL NAMES, not prose.** A natural-language question returns a
confident, well-formatted payload about the *wrong* symbols (measured: 0 of 7
relevant symbols, while the prose answer still needed the naive reads on top). If
you don't know the names yet, spend one `search_files` on the definition first, then
hand those names to the graph.

Keep plain `search_files` / `read_file` for one-line lookups — don't spend a dense
graph payload on them. CLI-only extras: `codegraph node|callers|callees|impact|
affected <symbol>`, `codegraph sync` when the graph looks stale.
<!-- /codegraph -->
