# Prompts

One file per prompt version: `prompts/<name>/v<N>.yaml`. The active version of each prompt is
chosen in `config/config.yaml` under `prompts:`.

To change a prompt:

1. Copy the current file to the next version (`v1.yaml` -> `v2.yaml`), set `version: 2`, edit
   `template`, and say what changed in `changelog`. Never edit a version that has been used:
   its ID is recorded in traces, the audit log, eval runs and chunk payloads.
2. Point `config/config.yaml` at the new version.
3. Run `python -m evaluation.run` and compare with the last run before merging.
4. Roll back by setting the old version number again.

Each prompt gets an ID like `answer@v2#1a2b3c4d` (the suffix is the first 8 hex characters of
the template's SHA-256), so an edit without a version bump still shows up as a different ID.
`variables` must list exactly the `{placeholders}` in the template; this is checked at startup.
