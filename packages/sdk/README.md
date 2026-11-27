# cliquet-sdk

The Cliquet SDK, imported as `cliquet`. An app is one short Python file written against it: actions change the data,
invariants say what must always hold, queries answer questions, and views lay out a screen.

This package also holds the runner, the test harness, the `cliquet` command, the worker that runs
app code, and the sandbox service. Apps depend on this package only.

Apps run only in a Cliquet server, and are written with its builder: see
[the repository's README](../../README.md). The `cliquet` command is for developing Cliquet.
