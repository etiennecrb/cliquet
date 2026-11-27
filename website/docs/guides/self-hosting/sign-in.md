---
icon: lucide/key-round
description: "Set up an OpenID Connect provider to sign people in to your Cliquet server, in the console and from MCP clients: Auth0, or the Keycloak that Docker Compose runs."
---

# Sign-in

Your server signs people in through an OpenID Connect provider, on its web pages and at its MCP server URL. The provider decides who may sign in. Your server decides who may build, as [Operations](operations.md#the-right-to-build) describes.

Cliquet asks of the provider only what OpenID Connect and OAuth define. It reads the provider's discovery document, at the issuer's `/.well-known/openid-configuration`, for everything else. Anything a provider does its own way, such as its emails, sign-in pages or password rules, is set up on the provider's side.

## Choose a provider

Any OpenID Connect provider signs people in on the web pages. Assistants need more: an MCP client such as claude.ai has no client ID at your provider, so the provider must register it the first time it connects. It does so in one of two ways:

- **Client ID metadata documents**, or CIMD: the client's ID is the address of a document that describes it, which the provider reads. MCP's current specification prefers this way.
- **Dynamic client registration**, or DCR: the client registers itself at the provider's registration endpoint. MCP keeps it as a fallback, for clients and providers without CIMD.

Your server registers no client and issues no token of its own. With a provider that offers neither way, people still sign in on the web pages, and assistants connect with an [API token](../../agents.md#api-tokens). The server then says so in its log as it starts:

```text
The sign-in provider <issuer> registers no MCP client: it offers neither dynamic client registration nor client ID metadata documents. Assistants can't sign in at <URL>/me/mcp; API tokens still work.
```

Some providers, as their documentation describes them:

| Provider | How it registers assistants |
| --- | --- |
| Auth0 | Both ways, once you turn them on, as [MCP clients](#mcp-clients) describes |
| Keycloak | Dynamic client registration, which the realm's client registration policies allow or refuse. CIMD is experimental: it needs the `cimd` feature and a client policy. [Set up Keycloak](#set-up-keycloak) allows claude.ai's |
| Google | Neither: assistants use API tokens |

Before you choose another provider, check which way its documentation says it supports. Each changes over time.

## Set up Auth0

These steps use Auth0 as the example. Another provider needs the same three things: an application with your callback URL, an API whose identifier is your MCP server URL, and the settings below.

<div class="steps" markdown>

1. **Create the application.**

    Create an Auth0 tenant, and a **Regular Web Application** in it. Add the callback URL and the logout URL, with your server's address in place of `https://cliquet.example.com`:

    ```text
    https://cliquet.example.com/auth/callback
    https://cliquet.example.com/
    ```

    For local development, also add `http://127.0.0.1:8800/auth/callback` and `http://127.0.0.1:8800/`.

2. **Create the API.**

    Add an API whose identifier is your server's MCP server URL, exactly, with no trailing slash:

    ```text
    https://cliquet.example.com/me/mcp
    ```

    Allow user-delegated access: choose **All apps allowed**, or authorize the application. Leave client and anonymous access off. Every MCP client asks Auth0 for a token for this API. The web sign-in asks for none.

3. **Choose a connection.**

    Enable Google, or the database connection with email and password. A person whose email isn't verified yet still gets in, with limits, as [Unverified emails](#unverified-emails) says.

4. **Choose who may sign in.**

    The first person to sign in may build, so sign in first yourself. Anyone after uses the apps shared with them, and builds only once you [grant it](operations.md#the-right-to-build), or from their sign-up with `CLIQUET_GRANT_BUILD_ON_SIGN_UP=1`. To keep a server closed, disable open sign-ups in Auth0 and create the people you trust.

    With open sign-ups, set `CLIQUET_SIGN_UP_PARAMS=screen_hint=signup`. A link to your server's `/signup`, such as a website's sign-up button, then opens Auth0 on its sign-up form instead of its sign-in form.

5. **Publish the sign-out endpoint.**

    In the tenant's **Settings**, **Advanced**, turn on **RP-Initiated Logout End Session Endpoint Discovery**. Signing out of Cliquet then signs out of Auth0 too. Without it, it signs out of Cliquet only.

6. **Set the server's settings.** Keep the client secret and `CLIQUET_SECRET_KEY` in your host's secrets: on Fly, see [Settings and secrets](fly.md#settings-and-secrets). [Settings](settings.md#sign-in) lists every sign-in setting.

    The issuer is your tenant's address, with its final slash, exactly as Auth0's tokens name it:

    ```text
    CLIQUET_AUTH=oidc
    CLIQUET_OIDC_ISSUER=https://<tenant>.eu.auth0.com/
    CLIQUET_OIDC_CLIENT_ID=...
    CLIQUET_OIDC_CLIENT_SECRET=...
    CLIQUET_SECRET_KEY=...
    ```

    `CLIQUET_SECRET_KEY` is a long random value of your own, at least 32 characters, such as one from `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.

7. **Deploy, and sign in.**

    Open your server's address and sign in. Then connect an assistant to its MCP server URL, ending in `/me/mcp`, and sign in from it.

8. **Link your privacy notice from Auth0's pages.**

    People sign in on Auth0's pages before they reach your server. Link the same notice as `CLIQUET_PRIVACY_URL` wherever Auth0's dashboard lets you, such as the pages' branding or text.

</div>

## Set up Keycloak

The public way of [Run with Docker Compose](compose.md) runs Keycloak next to your server, at its own name, such as `login.example.com`. At its first start, Keycloak imports the realm `cliquet`, from `docker/cliquet-realm.json`. The realm sets up:

- **The client `cliquet`**, for the web sign-in, with your server's callback and sign-out addresses, and the client secret from `.env`.
- **The audience.** Every access token the realm signs, for any client, names your server's MCP server URL, as your server requires.
- **Closed sign-up.** Nobody signs up alone: you add each person, as [Add a person](#add-a-person) shows.
- **Registration for claude.ai.** Keycloak registers an MCP client by itself only when the client's addresses are on `claude.ai`. The person then consents, the first time, to what the client reads.

Keycloak imports the realm once, with the values `.env` holds at that first start. To change `CLIQUET_BASE_URL` or the client secret later, change them in the realm too, with `kcadm.sh` as below. Both are in the client `cliquet`, and the address is also in the audience of the client scope `cliquet-mcp`.

<div class="steps" markdown>

1. **Fill in `.env`.**

    Set `LOGIN_HOST`, `POSTGRES_PASSWORD` and the `KC_BOOTSTRAP_ADMIN_` lines, as `.env.example` says, then the sign-in settings. The issuer is your `LOGIN_HOST` with `/realms/cliquet`, and no final slash:

    ```text
    CLIQUET_AUTH=oidc
    CLIQUET_OIDC_ISSUER=https://login.example.com/realms/cliquet
    CLIQUET_OIDC_CLIENT_ID=cliquet
    CLIQUET_OIDC_CLIENT_SECRET=...
    CLIQUET_SECRET_KEY=...
    ```

    Make the client secret, and each password, with `openssl rand -hex 32`. Empty `CLIQUET_DEV_LOGIN`, which `.env.example` sets for the local way: the server refuses it alongside the `CLIQUET_OIDC_` settings.

2. **Start the stack.**

    ```bash
    docker compose --profile public up -d
    ```

    Keycloak takes a minute or so at its first start. `docker compose ps` shows it as healthy once it is ready.

3. **Add yourself**, as [Add a person](#add-a-person) shows.

4. **Sign in.** Open your server's address and sign in. Keycloak asks you to choose a new password. As the first person on the server, you may build: others need a [grant](operations.md#the-right-to-build). A server you first ran the local way already has its first person, the development login, so grant yourself the right there.

</div>

### Use kcadm.sh

On the public name, Caddy answers `404` for Keycloak's admin console and its `master` realm, so nobody reaches them from the internet. Manage the realm with `kcadm.sh`, Keycloak's command line, in its container. Sign in first as the administrator that `KC_BOOTSTRAP_ADMIN_USERNAME` names, `admin` in the example. It asks for the password:

```bash
docker compose exec keycloak /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://localhost:8080 --realm master --user admin
```

Sign in again when a later command says the session expired. Keycloak's [admin CLI guide](https://www.keycloak.org/docs/latest/server_admin/#admin-cli) lists its other commands, such as deleting a person.

### Add a person

<div class="steps" markdown>

1. **Create the person**, with their email marked as verified:

    ```bash
    docker compose exec keycloak /opt/keycloak/bin/kcadm.sh create users -r cliquet \
      -s username=sam -s email=sam@example.com -s emailVerified=true -s enabled=true
    ```

2. **Give them a temporary password.** They choose their own at their first sign-in.

    ```bash
    docker compose exec keycloak /opt/keycloak/bin/kcadm.sh set-password -r cliquet \
      --username sam --new-password <temporary password> --temporary
    ```

3. **Send them** your server's address and the temporary password.

</div>

Your server trusts a verified email: the first sign-in with it claims the shares sent to that address. So check the address as you type it. A typo hands this person the shares meant for the mistyped address. Keycloak can check emails itself once you give it an email server, as its [guide](https://www.keycloak.org/docs/latest/server_admin/#_email) describes, but the stack needs none.

### Connect an assistant

In claude.ai, add a custom connector with your server's MCP server URL, such as `https://cliquet.example.com/me/mcp`, as [Connect your agents](../../agents.md#connect-an-assistant) shows. The realm lets Claude register at Keycloak. You then sign in, and consent to what Claude reads.

Keycloak refuses to register any other assistant, such as Claude Code, which signs in at an address on your own computer. Connect those with an [API token](../../agents.md#api-tokens). To let one register, add the host of its addresses to the realm's anonymous **Trusted Hosts** client registration policy.

Keycloak's default session lifetimes apply: an assistant signs in again once its session ends. Change them in the realm's session settings, such as `ssoSessionMaxLifespan`, with `kcadm.sh update realms/cliquet`.

### Remove old assistants

Claude registers a new client each time someone connects it, and Keycloak keeps the old ones. The realm takes at most 200 such clients: after that, Keycloak refuses to register Claude again. Before you reach it, count the clients named Claude:

```bash
docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get clients -r cliquet --fields id,name \
  | python3 -c "import json, sys; print(sum(c.get('name') == 'Claude' for c in json.load(sys.stdin)))"
```

Then remove them all. Each person who uses Claude connects it again once, and Claude registers a new client:

```bash
docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get clients -r cliquet --fields id,name \
  | python3 -c "import json, sys; [print(c['id']) for c in json.load(sys.stdin) if c.get('name') == 'Claude']" \
  | while read -r id; do
      docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh delete "clients/$id" -r cliquet
    done
```

## The issuer

`CLIQUET_OIDC_ISSUER` is the provider's issuer, exactly as its tokens and its discovery document name it, final slash included or not. A server whose discovery document names another issuer refuses every sign-in, and says which issuer to set. The server reads the discovery document as it starts, and again each hour. A server that can't reach its provider still starts: it reads the document again at the next sign-in.

## MCP clients

An MCP client registers with Auth0 itself the first time it connects. Your server is only the resource: it checks Auth0's access tokens against Auth0's published keys, its issuer and the API's identifier, and its protected-resource metadata names Auth0 as the issuer.

In the tenant's **Settings**, **Advanced**, turn on:

- **Resource Parameter Compatibility Profile**, so a client's `resource` parameter selects the API;
- **Include Issuer in Authorization Responses**, so a client can check the issuer, as your server does;
- **Dynamic Client Registration**, for clients such as Claude Code, and **Client ID Metadata Document (CIMD) Registration**, for clients such as ChatGPT. Turn on **Enable Application Connections** too.

An MCP client is a third-party application in Auth0. In the API's settings, allow user-delegated access for third-party applications, or authorize each registered client under the API's **Application Access**. Some tenants require a CIMD client to be imported first, under **Applications**, **Create Application**, **Import from URL**: ChatGPT's metadata URL is `https://chatgpt.com/oauth/client.json`.

Third-party applications can only use connections promoted to the domain level. Under **Authentication**, **Database**, **Username-Password-Authentication**, **Advanced Settings**, turn on **Promote Connection to Domain Level**, then enable the connection on the imported client, under its **Connections** tab. Without this, Auth0 answers `no connections enabled for the client`.

### Email in access tokens

A person can connect an assistant before they ever sign in on the web. The assistant then asks them to choose their login. Your server reads their verified email from the assistant's access token, or else from Auth0's userinfo endpoint with the same token.

Auth0 puts no email in its access tokens, and its userinfo endpoint refuses a token without the `openid` scope. Auth0 may grant a third-party client only `offline_access`. The person then gets in with no email, as if it weren't verified, and must sign in once in a browser within the days [Unverified emails](#unverified-emails) gives them.

To spare them that trip, add an Action that puts the email in every access token:

<div class="steps" markdown>

1. **Create the Action.** Under **Actions**, **Library**, click **Create Action**, then **Build from scratch**. Choose the **Login / Post Login** trigger.

2. **Write its code**, then click **Deploy**:

    ```js
    exports.onExecutePostLogin = async (event, api) => {
      api.accessToken.setCustomClaim("email", event.user.email);
      api.accessToken.setCustomClaim("email_verified", event.user.email_verified);
    };
    ```

3. **Add it to the login flow.** Under **Actions**, **Triggers**, open **post-login**, drag the Action into the flow, and click **Apply**.

</div>

The web sign-in needs none of this: it reads the email from the userinfo endpoint with a token of its own.

### Refresh tokens

The API allows offline access, so an assistant gets a refresh token and renews its 24-hour access token by itself. Auth0 gives a new client refresh tokens that never rotate and never expire. Once an assistant's client appears under **Applications**, open its **Settings**:

- under **Refresh Token Rotation**, turn on rotation, with a reuse interval of 30 seconds;
- under **Refresh Token Expiration**, set the idle lifetime to 45 days and the absolute lifetime to 365 days.

A monthly routine then keeps its access, and an assistant signs in again after a year, or after 45 days without a call. The web application needs no refresh token: leave the **Refresh Token** grant off, under its **Advanced Settings**, **Grant Types**.

## Unverified emails

A person whose email the provider hasn't verified yet still signs in, on the web or from an assistant, and chooses a login. Your server keeps no email for them until the provider verifies it. Until then:

- they claim nothing by email: no imported owner and no invitation, and a share sent to their email waits for it;
- they can't share their apps or make API tokens.

They have `CLIQUET_VERIFY_EMAIL_DAYS`, 7 by default, from when they chose their login. Their assistant and the console say how many days are left. After that, every page, tool and app URL refuses them until they verify it. Only **Your account** still opens, so they can download their apps, delete their account, or [ask for a new email](#send-the-email-again).

Your server sees the verified email at their next sign-in on the web, or in an assistant's access token. When the token carries none, it asks the provider's userinfo endpoint, at most once an hour per person.

!!! warning "Your provider must send `email_verified`"

    Your server takes an email as verified only when the provider sends the `email_verified` claim as `true`, beside the `email` claim, at its userinfo endpoint or in its access tokens. A provider that never sends that claim signs everyone in as unverified. Then, after `CLIQUET_VERIFY_EMAIL_DAYS`, your server refuses everyone, and signing in again doesn't help. Auth0 and Keycloak send it. With another provider, sign in once with a verified email, and check that the console shows it under your login, with no reminder to verify it. If it doesn't, set the provider up to send the claim before others sign in.

An account may be waiting for that email by then: an owner made by `cliquet-server import`, or a person whose identity a [move](#move-to-another-provider) cleared. If the person's own account holds nothing, no app, draft, app shared with them or API token, they become the waiting account. They get its login and apps, and keep their rights. Their own account is deleted, and its login stays reserved so that nobody takes it.

If their own account holds something, your server keeps both, and tells them once to ask you to join them. Your server never merges two accounts' apps. If another person who signed in has the email, the person keeps their account with no email, and is told so once.

### Send the email again

A person who lost the provider's email can ask for a new one in the console. **Send the email again** sits next to the reminder, and on the **Account** tab of **Settings** once their time is over. OpenID Connect has no request for this, so you name your provider's own in the settings. Without them, the console shows no button.

Your server first gets a token of its own, by OAuth's client credentials grant at the provider's token endpoint, with your client ID and secret. It keeps the token in memory until shortly before it expires. Then it sends the request you name, with `{subject}` replaced by the person's subject at the provider. Each person gets one email every 5 minutes at most.

=== "Auth0"

    <div class="steps" markdown>

    1. **Let the application use the client credentials grant.**

        In the application's **Settings**, under **Advanced Settings**, **Grant Types**, turn on **Client Credentials**.

    2. **Let it send verification emails.**

        In the application's **API Access**, authorize the **Auth0 Management API**, with the `update:users` permission only.

    3. **Set the server's settings.**

        ```text
        CLIQUET_VERIFICATION_EMAIL_URL=https://<tenant>.eu.auth0.com/api/v2/jobs/verification-email
        CLIQUET_VERIFICATION_EMAIL_BODY={"user_id": "{subject}"}
        CLIQUET_VERIFICATION_EMAIL_AUDIENCE=https://<tenant>.eu.auth0.com/api/v2/
        ```

        The URL's host is your issuer's, even a custom domain, since the token comes from there. The audience is always the tenant's own address.

    </div>

    `update:users` lets the application change any person at Auth0, so keep its secret safe, as before. Auth0 counts machine-to-machine tokens on some plans. Your server takes at most one a day, and one after each restart.

=== "Keycloak"

    Keycloak sends the email only once the realm has an email server, as its [guide](https://www.keycloak.org/docs/latest/server_admin/#_email) describes.

    <div class="steps" markdown>

    1. **Turn on the client's service account.** Find the client's ID, then update it:

        ```bash
        docker compose exec keycloak /opt/keycloak/bin/kcadm.sh get clients -r cliquet \
          -q clientId=cliquet --fields id
        docker compose exec keycloak /opt/keycloak/bin/kcadm.sh update clients/<id> -r cliquet \
          -s serviceAccountsEnabled=true
        ```

    2. **Let it send verification emails.**

        ```bash
        docker compose exec keycloak /opt/keycloak/bin/kcadm.sh add-roles -r cliquet \
          --uusername service-account-cliquet --cclientid realm-management --rolename manage-users
        ```

    3. **Set the server's settings.**

        ```text
        CLIQUET_VERIFICATION_EMAIL_URL=https://login.example.com/admin/realms/cliquet/users/{subject}/send-verify-email
        CLIQUET_VERIFICATION_EMAIL_METHOD=PUT
        ```

    </div>

    `manage-users` lets the client change any person in the realm, so keep its secret safe, as before.

When the provider refuses, the person is told to try again later, and your server's log says what the provider answered.

## Move to another provider

Your server knows each person by their provider's issuer and their subject there. A new provider, or a new custom domain at the same one, is a new issuer. Each person's first sign-in through it is then refused, because another identity already uses their email.

To move, clear every person's identity, and keep their email. Only an email claims an account back after the move, so give everyone an email first:

<div class="steps" markdown>

1. **List the people with no email.** Run this statement on the server's database:

    ```sql
    SELECT login FROM people WHERE deleted_at IS NULL AND email IS NULL;
    ```

    With the default SQLite database, run it from Python on the server's machine, as the image's user, as [Run a command](fly.md#run-a-command) shows on Fly:

    ```bash
    python -c "import sqlite3; print(sqlite3.connect('/data/server.db').execute('SELECT login FROM people WHERE deleted_at IS NULL AND email IS NULL').fetchall())"
    ```

2. **Ask them to verify their email.** Each verifies it with the current provider, then signs in once: their account then has it, unless another account has it already. A person with no email can't be claimed back after the move: their first sign-in through the new provider makes a new account.

3. **Delete the empty accounts.** List the people with no email again. Delete the accounts among them that hold nothing, as [Delete an account](operations.md#delete-an-account) shows.

4. **Switch the server to the new provider.** Set its `CLIQUET_OIDC_` settings, and deploy.

5. **Clear the old identities.** Run this statement on the server's database, with the new issuer:

    ```sql
    UPDATE people SET issuer = NULL, subject = NULL
    WHERE deleted_at IS NULL AND issuer IS NOT NULL AND issuer != '<new issuer>';
    ```

    With the default SQLite database, run it from Python on the server's machine, as the image's user, as [Run a command](fly.md#run-a-command) shows on Fly:

    ```bash
    python -c "import sqlite3; db = sqlite3.connect('/data/server.db'); db.execute('UPDATE people SET issuer = NULL, subject = NULL WHERE deleted_at IS NULL AND issuer IS NOT NULL AND issuer != ?', ('<new issuer>',)); db.commit()"
    ```

6. **Tell people to sign in once in a browser, or connect their assistant again.** Each person's first sign-in with their verified email claims their account, with its login, apps and shares. Their assistants then connect again, and their API tokens keep working. A person whose email the new provider hasn't verified yet gets in with a new account, which becomes their old one once they verify it, as [Unverified emails](#unverified-emails) says.

</div>

## Delete a person at the provider

Cliquet never calls the provider: when an account is deleted, it only forgets the person's identity. Delete the person at the provider too, or they can sign in again, as someone new. See [Delete an account](operations.md#delete-an-account).

Auth0's free plan includes 25,000 monthly active users and one tenant, which is enough for a small server.
