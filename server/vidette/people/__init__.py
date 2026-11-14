"""Known people: local face recognition that tells the judge who it recognized.

People the household enrolled (with their consent) are kept in a folder outside any git
repository, readable by its owner only: reference photos, aligned face crops, their
embeddings and the confirmations given later. A face check looks at frames of a clip,
finds faces (YuNet), turns readable ones into embeddings (SFace), compares them with the
enrolled people and returns a `PeopleReport`: who was recognized and how sure, how many
faces matched nobody, and how many frames showed no face that could be told at all.

What the judge gets is one line of facts. Only names of people actually recognized go
into it, and only for a local model: a cloud model is told "a known person", never a
name. Reference photos and embeddings never leave the machine.

Design and guardrails: docs/architecture/adr/0012-known-people.md, docs/people.md.
Only `vidette.people.engine` needs OpenCV (`pip install 'vidette[people]'`); the store,
the report and the matching are plain Python.
"""
