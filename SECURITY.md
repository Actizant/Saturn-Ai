# Security

Saturn is a program that can move your mouse, read your screen and run code on
your account. Treat that as the design constraint, not a footnote. This page is a
straight description of the trust boundaries as they exist today — including the
places where the protection is thinner than you would like.

## Versions we cover

Only the current release line receives security work. If you are running an older
checkout, update before reporting anything that looks like a bug.

## Telling us about a problem

Please do not open a public issue with an exploit, a proof-of-concept or anyone's
credentials in it. Use a private channel instead: GitHub security advisory against
`github.com/Actizant/Saturn`, or message the maintainers directly. If you
accidentally posted something sensitive publicly, delete it and rotate the secret
immediately — an edited comment is still in the activity log.

There is no bug bounty. Reports get fixed, credit if you want it, and our thanks.

## The trust boundary that matters most

Anyone who can reach the machine as your Windows user is already you as far as
Saturn is concerned. File permissions on the OS account, screen lock and disk
encryption are the real defences here; Saturn adds no wall between "you" and
"somebody sitting at your keyboard". Physical and account access are out of scope
for this document by design.

## Credentials

- Provider keys (`gemini_api_key`, `openrouter_api_key`) live in
  `config/api_keys.json` under `%LOCALAPPDATA%\SaturnAI\`.
- That file is **plain text**. There is no vault, no OS keychain and no
  per-user encryption around it. Anyone able to read your profile directory can
  read your keys.
- It is git-ignored and must never be committed. When pasting logs into an issue,
  scrub it first.
- Rotate a key the moment you suspect it leaked; Saturn does not detect misuse.

On Android the companion keeps its key and device secret in Jetpack DataStore
backed by the Android Keystore, which is a materially stronger place to put
them.

## Saturn Connect — the local gateway

The gateway (`saturn_connect/`) is how the phone companion talks to the desktop.
Defaults from `config/saturn_connect.json`:

| Setting | Default | Consequence |
| :--- | :--- | :--- |
| host | `0.0.0.0` | Reachable on every interface unless the firewall says otherwise |
| port | `8765` | One open TCP port on your machine |
| advertise | `true` | Broadcasts `_SATURN._tcp.local.` over mDNS when Zeroconf is installed |
| pairing_ttl_seconds | `300` | A pairing offer is valid for five minutes |

Read the consequences honestly:

- **The websocket is not encrypted.** There is no TLS on `/ws`. Pairing and
  device traffic are plaintext on your LAN. This is a known gap, not an
  oversight you should assume is hidden.
- **The admin REST endpoints have no authentication.** `/gateway/info`,
  `/gateway/pair`, `/gateway/devices`, `/gateway/devices/{id}/revoke`,
  `/gateway/devices/{id}/forget`, `/gateway/pending`,
  `/gateway/pending/{id}/approve` and `/gateway/pending/{id}/reject` are served
  on the same port with nothing in front of them. Anything that can open a socket
  to that port can approve its own device.

What *is* enforced: pairing requires a 6-digit code plus an explicit approve or
reject on the desktop; offers expire; a paired device authenticates with a
`device_id` and a secret the desktop stores only as a SHA-256 hash and compares
with `hmac.compare_digest`; revoked devices are refused at
`DeviceManager.authenticate`.

### Hardening it today

1. Firewall port `8765` to your LAN (or to the phone's address) — the gateway
   will not open or close OS rules for you.
2. Set `"advertise": false` unless you actually want discovery broadcasts.
3. Do not enable the gateway on a café, conference or airport network.
4. Review `/gateway/devices` periodically and forget anything you do not
   recognise.
5. If you need remote access, put a TLS-terminating reverse proxy in front of the
   gateway and bind it to loopback. Do not port-forward `8765`.

## Dashboard

The built-in dashboard (`dashboard/server.py`) listens on `8000`, with a legacy
HTTPS alias on `8001`. Its `_ensure_network_access` helper will try to *create* a
Windows firewall rule so the panel is reachable from the LAN — that is deliberate
but worth knowing about if you did not expect an app opening a port on your
behalf. Commands typed into the dashboard are sent AES-256-CBC with a key derived
per session; that salted transport keeps casual eavesdropping off the local
network, but it is not a substitute for authentication on a network you do not
control.

## Self-extension

Saturn can write new capabilities for itself when it meets a task it cannot do.
New code is executed in the Crucible sandbox before it is hot-registered, and a
failure there stops the tool from being kept. Understand what that means for your
threat model: a self-authored tool that survives the sandbox then runs with your
full user privileges. Judge the tools it grows, and the `features/` directory is
auditable like any other code.

## Autofix

The auto-heal loop patches runtime errors in its own source so a crash does not
end the session. It edits files on your disk. Keep it on a machine you control,
and keep the repository under version control so its edits are reviewable.

## Known limitations, listed plainly

- No TLS on the gateway websocket or the admin REST endpoints.
- No authentication on those admin endpoints.
- API keys in plain text on desktop.
- Gateway binds all interfaces by default.
- Dashboard may add a firewall rule without a separate confirmation.
- Anything that runs as your user can drive Saturn through its local interfaces.

## Recommendations

Run it where you would run a remote-desktop host: your own machine, your own
trusted network, your own account. Keep `config/api_keys.json` out of version
control, restrict `8765` and `8000` with the OS firewall, pair devices only when
you are somewhere you trust, and revoke promptly. If you add TLS or an auth layer
to the gateway, that is a change worth a pull request.
