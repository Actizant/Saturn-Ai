# Privacy

Saturn runs on your machine and keeps what it learns there. There is no account
to make, no profile to build, and no telemetry leaving your disk. This document
says exactly what crosses the wire and why.

**Last updated:** 27 October 2026

---

## Where Saturn lives

Everything Saturn remembers — settings, your key, conversation history, the
things it has noticed about how you work, files it produces — is written under
`%LOCALAPPDATA%\SaturnAI\` (Windows), the equivalent application-support folder
on macOS, and `~/.config/SaturnAI` on Linux. Delete that folder and Saturn
becomes a blank install. Nothing needs to be revoked on a server because there is
no server.

## What stays on your machine

- Your AI provider keys. On desktop they sit in `config/api_keys.json` inside
  that folder, in plain text, protected by your operating system's file
  permissions rather than by an app-level vault. On the phone companion they go
  into Android's encrypted DataStore. Treat the folder as private either way:
  never commit it, never paste it into an issue.
- Memory and learned rules. The cortex that holds your preferences, projects and
  instructions is a local store. It is not synced, backed up or shipped anywhere.
- Screen, camera and microphone material you did not ask to send. Frames are
  grabbed into memory, used, and dropped.
- Anything Saturn built for itself: skills it wrote, documents, images, circuit
  diagrams and generated firmware all land in your own folders.

## What leaves your machine, and only when you ask

Speaking to Saturn, showing it your screen, or pointing the camera at something
sends that content to the AI provider whose key you supplied — Google Gemini
first, OpenRouter if you configured a fallback — over HTTPS, for as long as that
request or live session takes. That is the whole point of the product, and it is
also the whole extent of it: whatever the provider's own terms say about that
traffic is between you, your key and them. Saturn adds no analytics SDK, no
crash reporter, no advertising identifier and no usage beacon of its own.

The one exception is opt-in and obvious: updates. Checking for a new version
fetches a manifest from `github.com/Actizant/Saturn`. Disable the updater and
even that stops.

## Pairing a phone

Saturn Connect puts a websocket gateway on your local network so your phone can
be a microphone, a camera and a second screen. Pairing needs a short-lived
6-digit code you approve on the desktop. After that the phone holds a device
secret and the desktop stores only its SHA-256 hash. Traffic between them stays
on your network — it is not relayed, and we never see it. Note the honest
caveat, also written in [SECURITY.md](SECURITY.md): that LAN traffic is not
TLS-encrypted today, so pair on networks you trust.

## Permissions

Saturn asks for microphone, camera, and — on the phone — accessibility and
notification access. Not out of curiosity: it needs them to hear you, to look at
what you show it, and to carry out the automations you name. It does nothing with
them while idle beyond the sentinels you switch on.

## Your machine, other people's data

Saturn is a powerful tool on a shared computer. Anyone with your Windows account
can speak to it, and it can read what is on your screen. If somebody else uses
your machine, their work can end up in Saturn's context. That is a property of
any assistant with eyes on your desktop, and it is your call who sits at that
keyboard.

## Children

This is not a product for under-13s, and we do not knowingly take information
about children.

## Asking questions

Write to us through the repository: https://github.com/Actizant/Saturn

If this policy changes, the date above changes and the new text lives here.
