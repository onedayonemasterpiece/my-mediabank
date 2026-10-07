# My MediaBank Android MVP

Thin native photo source and black WebView shell. Voice capture, playback, Stop,
Mira and WSS are provided by the backend's shared `live-interaction` browser
client. Native code does not call a provider or upload originals.

## Build

Use JDK 17, Gradle 8.10.2 and Android SDK 35 / build-tools 35.0.0, matching the
Projects Hub Android build. That project installs Gradle via its build runner,
so this project likewise does not vendor a wrapper binary.

```sh
gradle -p android testDebugUnitTest assembleDebug
```

`BACKEND_URL` can be supplied as a Gradle property or environment variable. It
must be HTTPS without credentials, query or fragment. Default:
`https://my-mediabank.kenigevents.ru/`.

```sh
gradle -p android assembleDebug -PBACKEND_URL=https://example.test/
```

Release builds use the externally supplied `ANDROID_KEYSTORE_PATH`,
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD`.
No signing material belongs in source. `versionCodeOverride` and
`versionNameOverride` are optional Gradle properties. A debug APK is signed with
the build machine's debug certificate; later upgrades require the same signing
identity, so retain the chosen signer outside the public repository.

## Browser bridge

`MediaBankAndroid` exists only in the exact configured HTTPS origin. Every
request must also come from the main frame; cross-origin pages and iframes
cannot request native photo or microphone access.

```js
MediaBankAndroid.postMessage(JSON.stringify({action: 'photo', index: 0}));
MediaBankAndroid.postMessage(JSON.stringify({action: 'permissions'}));
MediaBankAndroid.postMessage(JSON.stringify({action: 'app_info'}));
MediaBankAndroid.postMessage(JSON.stringify({action: 'settings'}));
```

Callbacks on `window.MyMediaBank`:

- `onPhoto({id, index, total, name, takenAt, width, height, dataUrl, access})`.
  `takenAt` is milliseconds since epoch; `access` is `full` or `selected`.
- `onNativeError({code, message, index})`. Codes include `permission_required`,
  `permission_denied`, `no_photos`, `end_of_photos`, `photo_unavailable`,
  `low_memory`, `microphone_denied`, `settings_unavailable`.
- `onAppInfo({versionName, versionCode, photoAccess, microphone})` and
  `onPermissions(...)` with the same shape. Photo access is `full`, `selected`
  or `denied`; microphone is `granted` or `denied`.
- `onBackground()` and `onForeground()`. The browser client must immediately
  stop capture/playback and its WSS session when backgrounded, and expose an
  explicit restart on return.

Runtime permission overlays preserve the first microphone request. Actual
backgrounding (including Home while a permission dialog is open) stops it.

The first denied photo request starts the system permission dialog once.
Explicit `permissions` requests let Android 14+ users choose/reselect photos.
After a successful permission selection the native shell loads index 0.
Permanently denied permission is actionable through Android application
settings. The OS microphone permission is requested only when the trusted
page requests `AUDIO_CAPTURE`; all other WebView permissions are denied.

Photos are ordered by capture time (date-added fallback), then stable MediaStore
ID descending. Only metadata is enumerated. The snapshot refreshes on resume
and permission changes, so after returning from another app an index can refer
to a new photo; use `id` to correlate an assessment. Decoding is asynchronous
with at most one current decode and one pending request. Older results are
discarded after navigation, a newer request or backgrounding.

Images retain their aspect ratio and EXIF orientation. Android 9+ uses
`ImageDecoder`, including device-supported HEIC. Android 8 uses bounded sampled
`BitmapFactory` decode plus EXIF transforms. Long side is at most 2048 px; the
JPEG uses quality 85 and is further reduced in dimensions if it exceeds 2 MiB.
The JPEG is provided as a browser data URL, with no disk thumbnail cache. Only
the browser's authenticated WSS flow may transmit it.

This MVP has no photo writes/deletes, gallery cleanup, videos, archives,
Telegram upload, original backup or background synchronization. No provider
key, backend access token or permissive TLS rule is embedded in the APK.
