# Saturn Connect Android

Android agent for Saturn Connect.

What this project is:
- Native Android agent
- WebSocket client for the existing Saturn Gateway
- Minimal pairing and reconnect flow
- First command set only

What this project is not:
- Not an AI assistant
- Not a second dashboard
- Not a cloud service
- Not a Windows or Companion build

Build requirements:
- Android Studio or Android Gradle Plugin toolchain
- JDK 17
- Android SDK 35

Phase scope:
- Gateway discovery
- QR pairing
- Secure credential storage
- Persistent WebSocket connection
- Battery, flashlight, launch app, open URL, and volume commands

## Signing, and why Play Protect blocks the debug APK

An APK signed with the generic Android debug key is an *unknown developer*. Add
the accessibility service (`BIND_ACCESSIBILITY_SERVICE`) and cleartext LAN
traffic, and it is exactly the profile Play Protect is built to stop - the same
warning Tasker, scrcpy and MacroDroid get. Nothing about the code is wrong; the
build simply has no identity.

Create the key **once** and keep it. Every future update must be signed with it,
otherwise Android refuses to install over the old copy (uninstall first, which
loses the pairing).

Needs a JDK 17 for `keytool` (`winget install EclipseAdoptium.Temurin.17.JDK`):

```
keytool -genkeypair -v -keystore saturn-release.jks -storetype JKS \
  -keyalg RSA -keysize 2048 -validity 10000 -alias saturn
```

Then write `keystore.properties` next to this README (git-ignored):

```
storeFile=saturn-release.jks
storePassword=<the keystore password>
keyAlias=saturn
keyPassword=<the key password>
```

`storeType` defaults to `JKS`; add `storeType=PKCS12` if you made a PKCS12 file.

Build:

```
./gradlew assembleRelease
# -> app/build/outputs/apk/release/app-release.apk
```

Without `keystore.properties` the release build still produces an installable
APK, but it falls back to the debug key and Play Protect will flag it again.

### From CI

The `build-all-platforms.yml` workflow has a `build-android` job that signs the
APK from four repository secrets:

| Secret | Value |
| --- | --- |
| `SATURN_KEYSTORE_BASE64` | base64 of `saturn-release.jks` (see below) |
| `SATURN_KEYSTORE_PASSWORD` | `storePassword` |
| `SATURN_KEY_ALIAS` | `keyAlias` (`saturn`) |
| `SATURN_KEY_PASSWORD` | `keyPassword` |

```
# PowerShell, from this folder
[Convert]::ToBase64String([IO.File]::ReadAllBytes("saturn-release.jks")) | Set-Clipboard
```

With no keystore secret the job still builds an APK, and warns that it carries
the debug key.
