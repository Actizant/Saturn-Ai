# Contributing

Saturn is open for tinkering. Read the code, run it, break it, send improvements
back. This is how we would like contributions to go.

## Get oriented first

Skim the [README](README.md) so you know what the pieces are for, and honour the
[Code of Conduct](CODE_OF_CONDUCT.md) in every thread. Before you write anything,
get Saturn running on your own machine with `python setup.py` and a Google AI
Studio key — a change you cannot run locally is very hard to review.

## Respecting the license

Saturn is open source under the [Apache License, Version 2.0](LICENSE). You are
free to use, study, modify and redistribute it, and any pull request you send is
offered under that same license. What Apache never grants is permission over the
Saturn name or logo — those belong to Actizant and stay governed by
[TRADEMARK.md](TRADEMARK.md), so contributing never gives anyone the right to
rebrand the project.

## The ground rules that save everyone time

- Never commit `config/api_keys.json`, tokens, or anything secret. It is ignored
  for a reason; double-check `git status` before you push.
- One idea per branch. A pull request that fixes the globe *and* rewrites the
  memory layer is two reviews pretending to be one.
- Add or adjust a test with your change. The suite (`py -3.14 -m pytest`) is the
  contract that keeps the assistant's self-healing and self-extension honest — a
  green suite is the fastest possible review.
- Match the style of the file you are editing rather than importing your own.

## How a change lands

1. Branch off `master`, name it for what it does.
2. Make the change, run the app, run the tests.
3. Open a pull request that says *why*, not just *what* — a line of intent is
   worth ten lines of "changed X".
4. Answer questions and push fixes on the same branch; the review stays in place.

A maintainer will look it over, and if it fits the direction of the project it
gets merged. Nothing is guaranteed and nobody is obligated, so give context and be
patient.

## When something is broken

For bugs, share the steps that reproduce it, what you expected, what actually
happened, and any log or screenshot. For anything security-related, skip the public
issue and follow [SECURITY.md](SECURITY.md) instead.

## Ideas and bigger work

If a change is more than a few commits, open an issue first and talk it through
before building it. The worst outcome is a well-crafted pull request for a
direction the project is not going. Saturn grows its own tools on purpose — so
should its roadmap get discussed before someone spends a weekend on it.
