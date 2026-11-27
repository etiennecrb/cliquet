---
icon: lucide/rocket
description: "Sign in, connect your AI assistant to Cliquet, and build your first app."
---

# Quickstart

Sign in, connect your AI assistant, and ask it for your first app. It takes a few minutes.

Cliquet deploys and hosts apps for you and the people you work with. Your assistant builds each app around your data, such as a spreadsheet, with the rules that data must follow. Then you, your team and your agents use the app, and its rules hold whoever makes a change.

<picture class="cq-diagram"><source media="(max-width: 40em)" srcset="/docs/assets/diagrams/how-it-works-tall.svg"><img src="/docs/assets/diagrams/how-it-works.svg" alt="You describe what you need to your assistant. It builds and uses your Cliquet app, which keeps your data, its rules and its screens. Your team uses the screens."></picture>

## Before you start

You need:

- an account at [app.cliquet.dev](https://app.cliquet.dev), which anyone may create, as the next step says;
- an AI assistant that connects to MCP servers, such as Claude, ChatGPT, Claude Code, Cursor or VS Code. MCP, the Model Context Protocol, is the standard that lets an assistant use tools outside it, here your Cliquet apps.

## Sign in

<div class="steps" markdown>

1. **Open [app.cliquet.dev](https://app.cliquet.dev).**

    It opens the sign-in page. Sign in with your email address, or click **Sign up** the first time. The sign-in page belongs to Auth0, the service that signs people in to Cliquet. If Auth0 hasn't verified your email yet, you can still go on: see [Verify your email](#verify-your-email).

2. **Choose your login.**

    The first time, Cliquet suggests a login from the part of your email before the @: `sam` for sam@example.com, or `sam-4k2` if someone has `sam` already. Keep it, or type your own, with lower-case letters, digits and hyphens. Then click **Continue**.

</div>

!!! warning "Your login is permanent"
    Your login appears in your apps' URLs and in their history, and the people you share with see it instead of your email. If the start of your email is your name, they see your name. You can't change it later.

You now see the console: the website where you find your apps, share them and make API tokens. Until you publish your first app, its **Apps** page shows how to build one, step by step.

You can also connect your assistant first. It then asks you to choose your login, with the same rules.

## Verify your email

Cliquet finds what is shared with you by your email, so it trusts only an email that Auth0 verified. Until Auth0 verifies yours, you use Cliquet with no email:

- nothing anyone shares with your email reaches you: it waits until you verify it;
- you can't share your apps;
- you can't make API tokens.

You have 7 days, from when you choose your login, to verify your email. Your assistant and the console say how many days are left. After that, Cliquet refuses everything until you verify it, and keeps your apps meanwhile. You can still [download your apps](guides/account.md#download-an-app) from the **Account** tab of **Settings**, [delete your account](guides/account.md#delete-your-account), and sign out.

<div class="steps" markdown>

1. **Open the link Auth0 sent you.**

    It verifies your email at Auth0. If you lost the email, click **Send the email again** next to the console's reminder, and Auth0 sends a new one. Once your 7 days are over, the button is on the **Account** tab of **Settings**.

2. **Sign in again at [app.cliquet.dev](https://app.cliquet.dev).**

    Cliquet reads your verified email, and what was shared with it then waits for you to accept.

</div>

If another account already has your email by then, your account keeps working with no email, and Cliquet tells you so once. What is shared with that email goes to the other account.

## Connect your assistant

Your assistant reaches Cliquet at one MCP server URL:

```text
https://app.cliquet.dev/me/mcp
```

In Claude, for example, open **Customize**, then **Connectors**, click **+ Add**, then **Add custom connector**. Name it `cliquet.dev`, paste the URL, and sign in when Claude asks. [Connect your agents](agents.md) has the steps for each assistant.

Check the connection by asking your assistant:

> List my Cliquet apps.

It answers with your apps, the ones people shared with you, and any share waiting for you. With a new account, it says you have none yet.

## Build your first app

Describe the app you want, and say what it must never allow:

> Create a Cliquet app for our office's meeting rooms, from my spreadsheet. Everyone books rooms. Only admins add rooms. Two bookings of a room never overlap.

Your assistant asks for your spreadsheet, writes the app and its tests, and shows you a preview. When you agree, it publishes the app. [Create and publish your app](create-and-publish.md) follows this example from start to finish.

## Next steps

<div class="grid cards" markdown>

-   :lucide-hammer: **[Create and publish your app](create-and-publish.md)**

    Turn a spreadsheet into an app, try it on a copy of your data, and publish it.

-   :lucide-users: **[Roles and sharing](sharing.md)**

    Give your team access, each with a role that says what they may do.

-   :lucide-history: **[History and undo](history.md)**

    See every change, undo one, or go back to an earlier version.

-   :lucide-layers: **[Architecture](guides/architecture.md)**

    What an app is made of, and the sandbox that keeps your data apart.

</div>
