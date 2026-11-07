# Saturn — Local Security Scan Report

> **Scope:** Static review of the repository at commit-time working tree.
> **Method:** Manual code review + pattern search (no cloud upload, no external scan engine, no Credits consumed).
> **Date:** 2026-11-06
> **Repo root:** the repository working tree at scan time (local absolute path omitted)

This is a **self-evolving desktop AI agent** (Python + PyQt6). Its entry points are
`main.py` (~349 KB), `ui.py` (~1.3 MB), `discord_bot.py`, and `llm_client.py`. The
security posture is unusual: the app's *core feature* is generating and running Python
code at runtime, so the analysis focuses on how that capability is bounded.

> ⚠️ **Correction note.** A previous set of notes for this task cited files named
> `app.py`, `evo_api.py`, `fsrc_api.py`, and `faciation_web.py` with specific line
> numbers. **Those files do not exist in this repository.** Every finding below has
> been re-verified against the actual code and cites real files/lines.

---

## 1. Summary

| Severity | Count | Themes |
|----------|-------|--------|
| High     | 2     | Runtime code execution boundary; TLS verification bypass guidance |
| Medium   | 3     | Arbitrary pip install at runtime; plaintext secret fallback (non-Windows); unpinned dependencies |
| Low      | 2     | CI workflow hardening; UI-only secret masking |
| Positive | 5     | Localhost-only servers; DPAPI secret sealing; default-deny `.gitignore`; approval-gate model; no hardcoded keys |

Overall this is a **security-conscious codebase** for its class: the authors explicitly
document that they *do not* pretend to have an OS sandbox (`core/permissions.py:19-22`),
seal secrets with Windows DPAPI (`core/secret_store.py`), and default-deny the entire
`config/` directory in `.gitignore`. The main residual risk is inherent to the product:
**LLM-authored code is executed on the user's machine**, and the guards around it are
advisory rather than enforcing.

---

## 2. Findings

### 🔴 HIGH-1 — LLM-generated code is executed with only a string-blocklist guard

**Where:**
- `core/skill_forge.py` → `SkillForge.forge_skill()` synthesizes Python from a natural-language goal, writes it to `features/{name}.py`, and hot-registers it (`core/skill_forge.py:133-221`).
- `core/skill_crucible.py` → `validate_ast()` is the "safety analysis" gate.
- `actions/desktop.py:78-96` → `exec(compile(code, ...), sandbox)` runs LLM-authored desktop code.

**What is actually enforced (`validate_ast`, `core/skill_crucible.py:99-131`):**
The only check is a scan of **string literals** for a small blocklist
(`BANNED_AST_PATTERNS`, `core/skill_crucible.py:26-42`): `format c:`, `diskpart`,
`system32`, `boot_sentry`, etc. Everything else passes.

**Why it's high:**
- Dangerous *behaviours* are not blocked — `os`, `subprocess`, `shutil.rmtree`, `socket`,
  `urllib`, `pickle` are all in `STANDARD_LIB_MODULES` (`core/skill_crucible.py:44-66`) and
  therefore permitted as imports. File deletion, data exfiltration, or reverse shells are
  never matched by the blocklist because it only inspects literal strings, not calls.
- Trivially bypassable: build the banned word at runtime (e.g. `"disk" + "part"`), or use any
  unlisted destructive API.
- The `desktop.py` "sandbox" (`actions/desktop.py:33-75`) exposes `getattr` and full `Path`/`os`
  objects, which defeats restricted builtins (classic `__class__.__mro__` escape).

**Mitigation present:** `core/permissions.py` implements an **approval gate** (Always/Ask/Block)
keyed on *declared* needs, and the code is honest that this is not an OS sandbox. For a
user-approved demo this is defensible; for an unattended/remote-triggered agent it is not.

**Recommendation:**
1. Run forged/derived code in a **real** isolation boundary (separate low-privilege process,
   container, or `firejail`/Windows AppContainer), not `exec` in the main process.
2. Replace the string blocklist with a **positive allowlist** of callable AST nodes/modules,
   or at minimum block `eval/exec/compile/__import__/os.system/subprocess/open(w)/socket` at
   the AST level.
3. Treat the approval gate as mandatory (deny-by-default) for any tool that declares
   FILES/NETWORK/COMMANDS.

---

### 🔴 HIGH-2 — TLS certificate verification is deliberately disabled in generated skills

**Where:** `core/skill_forge.py:279` and `core/skill_forge.py:394` (system prompt given to the code-generating LLM); echoed guidance in `core/mcp_forge.py:584,656`.

**What:**
The prompt instructs every forged skill to make HTTP requests with
`requests.get(..., verify=False)` or `ssl._create_unverified_context()`, justified as a
workaround for `[SSL: CERTIFICATE_VERIFY_FAILED]` on Windows.

**Why it's high:** Disabling certificate validation removes protection against
man-in-the-middle attacks for **all** network calls the agent makes — including any that
carry API keys, tokens, or user data. It is baked into the code generator, so it propagates
to every future skill automatically.

**Recommendation:** Fix the root cause (missing/expired CA bundle) instead of disabling
verification — e.g. pin `certifi` and pass `verify=certifi.where()`, or set
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`. Reserve `verify=False` for an explicit, per-call,
user-visible opt-in.

---

### 🟠 MEDIUM-1 — Runtime `pip install` of packages named by LLM output (no pinning/allowlist)

**Where:** `core/skill_crucible.py:156-200` (`resolve_dependencies`), used by the forge loop at `core/skill_forge.py:113-117`.

**What:** External import names extracted from generated code are passed straight to
`pip install <name>` (`core/skill_crucible.py:191`). There is no version pin, no index
allowlist, and no confirm-before-install for this step.

**Why it's medium:** This is a **supply-chain / typosquatting** vector: if the model emits a
misspelled or attacker-chosen package name (dependency-confusion), it is downloaded and then
*imported and executed* when the skill loads.

**Recommendation:** Install with pinned versions from a trusted index only, require explicit
user confirmation for new packages, and consider an allowlist of known-good names.

---

### 🟠 MEDIUM-2 — Secret at-rest protection silently falls back to plaintext on non-Windows

**Where:** `core/secret_store.py` (module docstring lines 21-27; `dpapi_available()` lines 55-60).

**What:** API keys / Instagram creds live in `config/api_keys.json`, sealed with Windows DPAPI
user-scope (strong) on Windows. On macOS/Linux, or on a `crypt32` failure, or when
`SATURN_SECRETS_PLAINTEXT=1`, the payload is written as **plain JSON** ("graceful degradation"
product rule, lines 21-24).

**Why it's medium:** The multi-platform build (`build-all-platforms.yml` targets macOS & Linux)
ships installers where those same "protected" secrets are actually on disk in cleartext.

**Recommendation:** Use a real cross-platform keystore (macOS Keychain / `keyring` /
`secretstorage` on Linux) rather than plaintext fallback. At minimum warn the user at rest and
restrict the file to `0600`.

---

### 🟠 MEDIUM-3 — Dependencies are almost entirely unpinned

**Where:** `requirements.txt` (45 lines; only `mcp>=1.2.0` has any bound).

**Why it's medium:** Unpinned direct **and transitive** dependencies make builds
non-reproducible and open the app to a malicious/compromised upstream release being pulled in
silently (see also MEDIUM-1, which amplifies this at runtime).

**Recommendation:** Pin with a lock file / hashes (`pip install --require-hashes`,
`pip-tools`, or `uv lock`) and enable Dependabot / `pip-audit` in CI.

---

### 🟡 LOW-1 — CI workflow lacks least-privilege `permissions` and dependency review

**Where:** `.github/workflows/build-all-platforms.yml`.

**What is good:** Android signing keys come from **GitHub Secrets** (`SATURN_KEYSTORE_*`,
lines 158-175) written to a temp file — no hardcoded secrets. ✅

**What to harden:**
- No top-level `permissions:` block, so the job runs with the default (broad) `GITHUB_TOKEN`.
- No `softprops/action-gh-release` signature/notarization step; artifacts (`Saturn-macOS.zip`,
  `Saturn-Linux.tar.gz`) are uploaded unsigned.
- `actions/checkout@v4`, `setup-python@v5`, etc. are not pinned to commit SHAs.

**Recommendation:** Add `permissions: contents: read`, pin actions to SHAs, and add a
`pip-audit` / CodeQL step.

---

### 🟡 LOW-2 — Some credential inputs are protected by UI masking only

**Where:** `ui.py:25801-25803` (Discord bot token field, `EchoMode.Password`); saved via
`_save_discord_settings` (`ui.py:18746-18760`).

**Note:** The token is stored in `discord_bot.json`, which **is** covered by `.gitignore`
(line 87) and the default-deny `config/*` rule (line 72) — so it is not committed. Password
echo is UI-only masking (not storage encryption); confirm this value routes through
`secret_store` like `api_keys.json` for at-rest protection on non-Windows (see MEDIUM-2).

---

## 3. Verified-good (no action needed)

- **Localhost-only servers.** `saturn_view/server.py` is a FastAPI app that serves a
  static globe tree + thin data-proxy endpoints; the HTTP host binds to `127.0.0.1`
  (ports probed on loopback in `saturn_view/window.py:216`). No CORS wildcard, no
  `0.0.0.0` bind, no auth is reasonable for a loopback-only surface. Query params are
  coerced with `float()` before use (`saturn_view/server.py:62-66`), so no injection
  into the upstream feeds. (Saturn Studio's former loopback server — `saturn_studio/server.py`
  and `saturn_studio/web_host.py` — was removed in the native-engine migration; the studio
  now renders in-process with no HTTP surface at all.)
- **No hardcoded secrets** matched across `.py` (api-key/token/password/secret literal scan).
- **`.gitignore` is default-deny** for `config/` (lines 63-78) and explicitly ignores
  `api_keys.json`, `email_credentials.json`, `discord_bot.json`, `ig_session.json`,
  `*.sqlite3`, `.env*`. Strong hygiene against accidental commits.
- **DPAPI user-scope** chosen over machine-scope deliberately (`core/secret_store.py:15-20`).
- **Honest security model** documented in `core/permissions.py:19-26` — the UI states what is
  actually enforced instead of claiming a sandbox.

---

## 4. Prioritized remediation

> **Status (this pass):** HIGH-2 ✅ fixed, HIGH-1 ✅ hardened, LOW-1 ✅ fixed.
> MEDIUM-1/-2/-3 left open deliberately — each needs a product decision or a
> cross-platform behavior change that is riskier than the hackathon timeline allows.

1. **HIGH-2** — ✅ **Fixed.** `skill_forge.py` codegen prompt now keeps TLS
   verification ON and prescribes `verify=certifi.where()` (with a guarded
   last-resort retry) instead of blanket `verify=False`, matching the already-safe
   `mcp_forge.py` policy.
2. **HIGH-1** — ✅ **Hardened.** `skill_crucible.validate_ast` now folds
   concatenated/f-string literals, flattens shell/subprocess argument lists, and
   rejects recursive deletes aimed at a drive/OS root — closing the `"disk"+"part"`
   and `["format","C:"]` bypasses while leaving ordinary temp-file cleanup working.
   (Full isolation via a real OS boundary remains the recommended follow-up.)
3. **MEDIUM-1 / MEDIUM-3** — Pin + hash dependencies and require confirmation
   before any runtime `pip install`. *(Open.)*
4. **MEDIUM-2** — Replace plaintext secret fallback with a cross-platform keystore.
   *(Open.)*
5. **LOW-1** — ✅ **Fixed.** CI now declares `permissions: contents: read` and runs a
   non-blocking `pip-audit` advisory step in the Windows build.

---

*Generated locally from manual review and pattern search. No code was uploaded and no
external SCA/SAST engine was run for this report.*
