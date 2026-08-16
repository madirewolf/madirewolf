# Reel Block

An Android app that blocks Instagram Reels, **except** the one reel a friend sent you in a DM. Swipe to the next one and the blocker kicks in immediately.

Not an all-or-nothing app blocker. The infinite feed is the problem; a link someone actually sent you is not.

Runs entirely on-device. No network calls, no analytics, nothing leaves the phone. MIT licensed.

---

## The rules

- Main Reels feed (the bottom-nav "Reels" tab): always blocked.
- A reel opened from inside a DM thread: you get to watch that specific reel.
- The moment you swipe to the next one: blocked.
- Leave Instagram and come back: state resets.
- The DM exemption is a switch on the home screen. Turn it off and every reel is blocked, DM-shared or not.

---

## What makes it interesting technically

Instagram is a closed app. There is no API for "what screen is on top". The Android Accessibility framework is the only supported way to read the active window's content on a non-rooted device, which makes this an exercise in inferring application state from a view tree you do not control and that changes without warning.

### Pure core, thin driver

The interesting logic is deliberately kept away from anything Android:

| File | Role | Android-aware? |
|---|---|---|
| `NodeSnapshot.kt` | Immutable POJO copy of an `AccessibilityNodeInfo` subtree | only at the copy boundary |
| `ReelDetector.kt` | Maps a snapshot to a `ScreenType` and extracts a reel signature | no |
| `SessionStateMachine.kt` | Decides `ALLOW_REEL` / `BLOCK_REEL` / `NONE` from the screen sequence | no |
| `ReelBlockerService.kt` | The AccessibilityService. Event plumbing, overlay, navigation, notification | yes, entirely |

`AccessibilityNodeInfo` is close to untestable: it needs the live accessibility framework and it recycles under you. Copying the four fields that matter (`viewIdResourceName`, `contentDescription`, `text`, `className`, plus `isSelected`, `isScrollable`, and children) into a plain data class means the detector and the state machine can be unit-tested on the JVM against hand-built trees with no mocking, no emulator, and no Instagram. Three test suites do exactly that, driven by a `NodeSnapshotFactory` of realistic fixtures, and they cover every ALLOW/BLOCK/NONE transition plus an integration pass that runs the detector and the state machine together.

### The swipe detection problem

The hard question is not "is this a reel". It is "**is this still the same reel**".

Two independent signals answer it, because either one alone has a failure mode:

1. **A `TYPE_VIEW_SCROLLED` event while in DM-allowed mode short-circuits to BLOCK.** This is the clearest possible signal that the user swiped, and it is the primary path.
2. **Signature comparison is the backstop,** for swipes the scroll signal misses, such as one inside the post-open grace window. On entry the service latches the reel's signature. If a later signature differs, the user moved to a different reel.

The subtlety is which signatures are allowed to trigger that comparison. A signature is either a stable `user:` prefix (the author's username) or a coarse `txt:` caption hash fallback. The caption hash drifts as text loads frame by frame, so comparing two `txt:` signatures produces false BLOCKs on the reel you are legitimately allowed to watch. So the comparison only fires when **both** the latched and the incoming signature are stable `user:` ones. A coarse latch gets upgraded in place the moment a stable signature appears, and a null latch gets filled by the first signature seen, because signatures often load late.

### Prefer `event.source` over `rootInActiveWindow`

The obvious way to snapshot the screen is `rootInActiveWindow`. It is frequently null during window transitions ([issuetracker #223809542](https://issuetracker.google.com/issues/223809542)), which is precisely when a Reels screen is being pushed. Signals were being lost at the exact moment they mattered.

The service prefers `event.source` and walks up from there, falling back to `rootInActiveWindow` only when it has to. Which source produced a given snapshot is recorded in the detection log, so a misclassification can be traced back to whether the tree was even available.

### Blocking is a sequence, and it verifies itself

On BLOCK the service drops a full-screen overlay so the reel is not visible, fires `GLOBAL_ACTION_BACK` with a short delay, and then **schedules a verification pass** with retries. Firing back once and hoping is not enough; Instagram sometimes eats the first one, or lands you on a screen you did not ask for. The service checks where you actually ended up and escalates, including clicking the last known non-Reels bottom tab directly when it can find it in the tree. The overlay auto-dismisses after about 1.2 seconds, and a cooldown prevents a block loop from fighting the user.

### Survivability

Instagram rotates its resource IDs across releases, which will eventually break any detector built on them. Two things blunt that:

- **`endsWith` matching**, so only the suffix after `com.instagram.android:id/` needs to be maintained, and the ID lists are grouped constants rather than scattered string literals.
- **A live detection log** in the app: a ring buffer of recent events with the classification, the signature, the decision, and a diagnostic summary of the tree. When blocking feels wrong on a new Instagram version, you reproduce the failure, open the log, and see exactly which screen classified as what. That turns a re-tune from a debugging session into reading a list.

Detection also does not depend on a single mechanism. Classification is a weighted combination of ID markers, reel action content-descriptions, video surface presence, story viewer markers, DM markers, bottom tab selection state, tree depth, and class names, plus a separate path that classifies directly off bottom-nav selection events. Losing one signal degrades accuracy instead of breaking the app.

### Privacy by construction, not by promise

```xml
<!-- app/src/main/res/xml/accessibility_service_config.xml -->
android:packageNames="com.instagram.android,com.instagram.android.debug,com.instagram.lite"
```

The service is scoped to three package names. It is not that the app chooses not to look at your banking app; the OS never delivers it those events. Verify it yourself in that one file, it is short.

---

## Decision flow

```
                 ┌─────────────────────┐
                 │ Any Instagram event │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │   classify screen   │
                 └──────────┬──────────┘
                            ▼
 REELS_TAB ───────────────► BLOCK
 OTHER / UNKNOWN ─────────► NONE    (preserve last-known screen)
 DM_THREAD / DM_INBOX ────► NONE    (reset mode to IDLE)
 REEL_VIEWER
   ├─ "Allow reels from DMs" off ─► BLOCK  (no exemption at all)
   ├─ prev = DM_THREAD  ─► ALLOW   (latch reel signature)
   ├─ mode = DM_ALLOWED
   │   ├─ same author sig ─► ALLOW
   │   └─ diff author sig ─► BLOCK  (user swiped to next reel)
   └─ else              ─► BLOCK
```

---

## Build and install

**Android Studio:** open the folder, let it fetch Gradle, the Android SDK 34, and Kotlin, then Sync and Run with a device connected over USB debugging.

**Command line** (needs JDK 17 and the Android SDK with `ANDROID_HOME` set):

```bash
gradle wrapper --gradle-version 8.7 --distribution-type bin   # one time
./gradlew assembleDebug        # macOS / Linux
gradlew.bat assembleDebug      # Windows
./gradlew installDebug         # to a connected device
./gradlew testDebugUnitTest    # unit tests
```

The APK is written to `app/build/outputs/apk/debug/app-debug.apk`.

---

## First-run setup on the phone

Two OS-level grants. The home screen walks you through both and each has a button that jumps straight to the right settings page.

1. **Display over other apps** (`SYSTEM_ALERT_WINDOW`), so the blocking cover can appear on top of Instagram.
2. **Accessibility Service.** Toggle *Reel Block monitor* on under Settings, Accessibility, Installed services.

Android will warn you that an accessibility service can "observe your actions" and "retrieve window content". Reel Block needs exactly that level of access to see which Instagram screen is on top. Because the service is scoped to Instagram's package names only, it cannot observe anything else.

Once both grants are green, flip "Blocking enabled" on and open Instagram. Tap **View live detection log** to watch screens classify in real time.

---

## Requested capabilities

Everything the manifest declares:

| Permission / capability | What for |
|---|---|
| `BIND_ACCESSIBILITY_SERVICE` (on the `<service>`) | Receiving Instagram's accessibility events. |
| `SYSTEM_ALERT_WINDOW` | Drawing the full-screen "Reel blocked" cover. |
| `POST_NOTIFICATIONS` | The ongoing status notification that mirrors the live decision state so you can see what the service is doing without leaving Instagram. |
| `RECEIVE_BOOT_COMPLETED` | Reserved for optional auto-start behaviour. Not currently wired up. |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | So aggressive OEM battery managers do not kill the service mid-session. |
| `<queries>` for the three Instagram packages | Package visibility on Android 11+, so the service actually receives Instagram's events. |

No internet permission. No device admin. No root.

---

## Tuning detection

Instagram's resource IDs rotate across releases. If blocking feels off on a new version:

1. Open the app, tap **View live detection log**.
2. Reproduce the failure in Instagram (for example, open the Reels tab).
3. Come back to the log. The mis-classified screen shows up as `OTHER` instead of `REELS_TAB`, or the reverse.
4. Add the new ID marker to the right list in `ReelDetector.kt`: `REEL_VIEWER_ID_MARKERS`, `BOTTOM_TAB_ID_MARKERS`, `DM_THREAD_ID_MARKERS`, or `DM_INBOX_ID_MARKERS`.
5. Rebuild and reinstall.

`endsWith` matching means you only need the suffix after `com.instagram.android:id/`.

---

## Layout

```
app/src/main/java/com/reelblock/app/
  ReelBlockerService.kt         the AccessibilityService (the large, impure one)
  ReelDetector.kt               pure classifier + signature extraction
  SessionStateMachine.kt        pure ALLOW/BLOCK/NONE policy
  NodeSnapshot.kt               testable copy of an AccessibilityNodeInfo subtree
  ScreenType.kt                 the enum
  BlockingOverlayController.kt  full-screen cover
  DetectionLog.kt               in-memory ring buffer of recent decisions
  MainActivity.kt               dashboard, permission flows, the two switches
  LogActivity.kt                live detection log + clipboard export
  SettingsStore.kt              SharedPreferences flags
  ServiceStatus.kt              permission helpers
app/src/test/java/com/reelblock/app/
  NodeSnapshotFactory.kt              hand-built view-tree fixtures
  ReelDetectorTest.kt                 classification
  SessionStateMachineTest.kt          every transition and edge case
  DetectorStateMachineIntegrationTest.kt
```

---

## Honest state

It works, on the Instagram versions it has been tuned against. Specifically:

- **The detection ruleset is empirical and dated.** It is reverse-engineered from a view tree Instagram can change in any release, with no contract and no notice. Expect to re-tune. The live log exists because of this, not in spite of it.
- **The service is not small.** `ReelBlockerService.kt` is around 1,450 lines. The pure core it drives is small (`SessionStateMachine.kt` at ~200 lines, `ReelDetector.kt` at ~535), but the Android-facing driver has accumulated the event plumbing, navigation verification, cooldown handling, notification state, and runtime service reconfiguration that a real accessibility service ends up needing. It is the part most in need of a refactor.
- **Test coverage stops at the boundary.** The pure classifier and state machine are well covered. The service itself has no tests, because testing it would mean testing the accessibility framework.
- **`RECEIVE_BOOT_COMPLETED` is declared but unused,** and a `FileProvider` is declared in the manifest that nothing currently uses. Log export goes to the clipboard, deliberately, to avoid share-sheet and MIME negotiation. Both are leftovers worth cleaning up.
- **Instagram only.** The architecture would extend to TikTok or YouTube Shorts, but nothing in the ruleset does today.

---

## License

MIT. Do whatever you like with this. If you extend the detection ruleset for a new Instagram release, a PR would be lovely.

---

## Screenshots

TODO. Save to `docs/img/` and put the first two side by side at the top.

1. **`blocked.gif`** — the core demo, and worth the effort to get right. Screen-record on a real device: open Instagram, tap the Reels tab, and capture the cover dropping and the app routing you back out. About 6 seconds. Use `adb shell screenrecord --time-limit 10 /sdcard/demo.mp4` then `adb pull`, and trim it. Nothing else explains the app this fast.
2. **`dm-exemption.gif`** — the feature that makes this different from every other blocker. Open a DM-shared reel (it plays), then swipe up (it blocks immediately). About 8 seconds. If you make only one recording, make it this one.
3. **`detection-log.png`** — the live log with a real scroll of classified events showing screen types, signatures, and decisions. This is the screenshot that tells another engineer the thing is debuggable.
4. **`home.png`** — the dashboard with both permission chips green and both switches on.

Blur or crop any DM sender names, avatars, and reel content before publishing. Use a throwaway account for the recordings if you have one.
