# Security

Cliquet keeps people's data, often personal or financial, and runs other people's app code in a sandbox. Reports of a weakness are welcome.

## Reporting a vulnerability

Report it privately, never in a public issue:

- through GitHub's private vulnerability reporting, under this repository's **Security** tab;
- or by email to hello@cliquet.dev.

Say what you found, how to reproduce it, and what it lets someone do. You get an answer as soon as the maintainer can give one. Please wait for a fix before you speak of it in public. Cliquet is a free beta kept by one person: there is no bug bounty.

## In scope

- App code that leaves its sandbox, or reaches the server, another person's sandbox or the network.
- A person, an app or a token that reaches data it was not given: another person's apps, another app's data, or more than its role or its token allows.
- Getting round sign-in, sharing or roles, and a session or a token taken or leaked.
- A write that gets round an app's rules, or changes its history without a trace.
- The hosted service at `app.cliquet.dev`, and the code in this repository.

## Out of scope

- Denial of service, load and rate limits.
- Bugs in Fly.io, Auth0 or a vendored library (daisyUI, Chart.js, htmx, Lucide) themselves: report them to their makers.
- A self-hosted server's own setup, such as its network, its secrets or its sign-in provider.
- Reports from automated scanners with no proof of an effect, and social engineering.
