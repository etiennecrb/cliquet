#!/bin/sh
# Wait for this setup's marker, then serve. A container made again waits for its own setup, so
# it never runs a line of the image's own SDK or of an earlier one.
set -e
while [ ! -e "/sandbox/ready-$CLIQUET_SANDBOX_SETUP" ]; do
  sleep 0.2
done
exec python -m cliquet.cli sandbox --uds /sock/sandbox.sock --root /sandbox
