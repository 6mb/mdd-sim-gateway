# Contributing

Open an issue before a large behavior or hardware change. Keep device operations fail-closed, never add real subscriber data to fixtures, and preserve upstream attribution.

Enable the hooks once per clone:

```bash
git config core.hooksPath hooks
```

`hooks/pre-push` refuses to publish a subscriber identifier it cannot recognise as fictional.
CI runs the same check, but a push cannot be taken back — and it scans the commits being
pushed rather than the working tree, because a value that was committed and then removed is
still in the history the push publishes.

Before submitting a change:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile control/app/*.py engine/*.py host/*.py
sh tools/check-subscriber-identifiers.sh
cd webui && npm ci && npm run build
```

Use focused commits, document user-visible changes in `CHANGELOG.md`, and add tests for routing, authentication, device state and secret redaction.
