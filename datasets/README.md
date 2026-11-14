# Datasets for `vidette eval`

Format, metrics and how to build your own set: [docs/eval.md](../docs/eval.md).

| Dataset | Entries | What it is | License |
|---|---|---|---|
| [synthetic-sample](synthetic-sample/labels.yaml) | 5 | Flat boxes drawn with ffmpeg plus hand-typed upstream facts. A format demo: its numbers say nothing about real footage. | Apache-2.0 |

Rules for anything added here:

- **No real footage of people, faces or number plates**, ever (see CLAUDE.md "Don'ts").
  Real sets live outside the repository; only their numbers get published.
- Every set states its `license` in `labels.yaml`, and its provenance in its description.
- Public-domain or permissively licensed clips are welcome only with a link to the source
  and its license next to them.
