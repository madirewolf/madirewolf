# Redline

An Android training app that listens and feels. Log a set by talking during your rest, and let the phone's accelerometer tell you how close the last set actually came to failure.

Native Kotlin and Jetpack Compose. Roughly 4,600 lines across 46 files. Hilt, Room, Material 3, Navigation-Compose, MVVM.

---

## The idea

Two things break workout logging. You cannot type with chalk on your hands, and "how hard was that set" is a number people guess at badly.

So: speech goes in and structured sets come out, and rep tempo gets measured off the accelerometer instead of self-reported. Everything is local. Nothing needs an account.

---

## What makes it interesting technically

### Voice parsing degrades in three tiers, and works offline

`VoiceParser.parse()` tries three strategies in order, each gated on a confidence threshold:

1. **Separator parse.** The structured format is `"{exercise} next {weight} next {reps} [next {notes}]"`. Splitting on a spoken separator and parsing each segment independently is far more reliable than parsing freeform speech, so this runs first and short-circuits at confidence ≥ 0.60.
2. **Remote LLM parse.** If a user has configured an endpoint in Settings, `RemoteVoiceParser` POSTs `{schemaVersion, transcript}` and expects back a typed object with `exerciseName`, `canonicalExerciseKey`, `weight`, `unit`, `reps`, `rpe`, `notes`, `confidence`, and `needsClarification`. Accepted at confidence ≥ 0.55 and only when `needsClarification` is false. The whole call is wrapped in `runCatching` and returns null on any failure, so a dead endpoint costs one timeout, not a crash.
3. **Local freeform regex.** Six compiled patterns with lookaround guards handle explicit units (`225 lbs`), prepositional weights (`at 225`), reps after `for`/`x`, RPE, and bare numbers, plus keyword lists that infer effort from words like "grind", "barely", "flew up".

The important property is the ordering. The LLM sits in the **middle**, not at the front and not as a hard dependency. With no endpoint configured, the app is fully functional on-device. The model is an accelerator for messy input, not the thing that makes logging work.

Parsed exercise names go through `ExerciseCatalog` to a canonical key, so "incline dumbbell press", "incline db press", and "incline press" all land on one row in the progress history.

### Tempo analysis is motion, not audio

`TempoRecorder` registers for `TYPE_LINEAR_ACCELERATION`, falling back to `TYPE_ACCELEROMETER` on devices without it, at `SENSOR_DELAY_GAME`. Each event becomes a `TempoSample` of `(timestampNs, sqrt(x² + y² + z²))`.

`TempoAnalyzer` then does the actual work:

- **Smoothing.** A 3-sample moving average, because raw accelerometer magnitude is noisy enough to produce phantom peaks.
- **Adaptive threshold.** `mean + max(0.18, 0.75 × stddev)`. Fixed thresholds fail across the range from a light warm-up to a heavy grind; scaling to the set's own variance does not.
- **Peak picking with a refractory window.** Local maxima above threshold, with a 450 ms minimum gap so one rep's acceleration and deceleration do not register as two reps.
- **Interval filtering.** Peak-to-peak intervals outside 0.35s to 8s are discarded as noise or as rack time.
- **Degradation.** Compare the mean interval of the first third of reps against the last third. Rep time growing across a set is the physical signature of approaching failure, and it is measured rather than asked about.
- **A failure-proximity score** banded off that degradation percentage, plus a confidence value that scales with how many usable intervals were found.

Every failure path returns a typed reason instead of a plausible-looking number: `"Not enough movement data"`, `"Movement was too subtle to score"`, `"Rep timing was inconsistent"`, each with confidence at or near zero. A tempo analyser that always answers is a tempo analyser that lies.

This is also the one component under unit test (`TempoAnalyzerTest`), which is deliberate: it is the only piece with enough numerical behaviour that a test tells you something a manual run would not.

### The permission surface is handled on purpose

Four sensitive grants, each with a rationale flow rather than a cold system prompt:

- `RECORD_AUDIO` for Android's on-device `SpeechRecognizer`. Audio never leaves the phone; only the transcript string does, and only if you configured a remote parser endpoint.
- `ACCESS_NOTIFICATION_POLICY`, so `DndManager` can set `INTERRUPTION_FILTER_NONE` when a workout starts and restore `INTERRUPTION_FILTER_ALL` when it ends. It checks `isNotificationPolicyAccessGranted` before every call and no-ops without it, and it can deep-link straight to the right settings page.
- `VIBRATE` for set and rest feedback.
- `INTERNET` only for the optional USDA nutrition lookup and the optional remote parser.

### Architecture

Clean MVVM with no shortcuts. Room database at schema version 3 with five entities (`WorkoutSession`, `ExerciseSet`, `PrTrack`, `MotivationalVideo`, `CalorieEntry`) behind a single `WorkoutDao`, wrapped by `WorkoutRepository`, injected into ViewModels by Hilt with KSP-generated components. Navigation-Compose with 11 routes including two parameterised ones (workout detail by session id, exercise history by canonical key, URL-encoded). Coroutines and `StateFlow` throughout; the sensor recorder exposes a sealed `TempoState` of `Idle | Recording | Ready | Error` rather than a bag of nullable fields.

Nutrition lookups hit USDA FoodData Central with a key the user supplies in Settings. Blank key means the feature returns an empty list rather than throwing.

---

## Stack

Kotlin, Jetpack Compose with Material 3, Hilt with KSP, Room, Navigation-Compose, Coroutines and Flow, Android `SpeechRecognizer`, `SensorManager`, Gradle Kotlin DSL with a version catalog. `minSdk 26`, `targetSdk 34`, JVM target 17, R8 minification on release.

---

## Run it

**Android Studio:** open the project folder, let Gradle sync, hit Run with a device connected over USB debugging.

**Command line:**

```bash
./gradlew assembleDebug      # macOS / Linux
gradlew.bat assembleDebug    # Windows
./gradlew installDebug       # to a connected device
./gradlew testDebugUnitTest  # unit tests
```

Requires JDK 17 and the Android SDK with `ANDROID_HOME` set. The APK lands in `app/build/outputs/apk/debug/`.

**First run on the phone:** grant the microphone permission when prompted, and grant notification-policy access from Settings if you want the do-not-disturb toggle to work. Two optional API keys go in the in-app Settings screen: a USDA FoodData Central key for calorie lookups, and an LLM endpoint URL for the remote voice parser. Both features degrade quietly when their key is blank.

---

## Layout

```
app/src/main/java/com/redline/app/
  data/local/           Room database (v3), 5 entities, WorkoutDao
  data/repository/      WorkoutRepository
  di/                   Hilt module
  voice/                SpeechRecognizerManager, VoiceParser, RemoteVoiceParser, ExerciseCatalog
  tempo/                TempoRecorder (sensor), TempoAnalyzer (the maths)
  nutrition/            USDA FoodData Central client
  settings/             SettingsStore
  util/                 DndManager
  ui/                   home, log, tempo, progress, calories, music, motivation,
                        social, settings, workout detail, exercise history,
                        navigation, theme, components
app/src/test/           TempoAnalyzerTest
```

---

## Honest state

Version 0.1.0. It builds, installs, and runs. What is not finished:

- **One unit test.** `TempoAnalyzerTest` covers the analyser. Nothing else has coverage.
- **The Social screen is a placeholder.** It renders a "Gym Finder" empty state asking for a Google Maps API key and a "Coming Soon" card. There is no gym discovery and no social graph behind it. The location permissions in the manifest are declared for that future feature and are currently unused.
- **`MusicController` is one `ACTION_VIEW` intent.** It hands a stored track URI to whatever app claims it. There is no playback integration, no Spotify SDK, no now-playing state.
- **The remote voice parser expects an endpoint you provide.** There is no hosted service, and the request schema (`schemaVersion: 1`) is defined by this app rather than by any standard.
- **No release signing config, no Play listing.** Debug builds only so far.

---

## Screenshots

TODO. Android screenshots need device frames or they look like bug reports. Capture on a real phone (`adb exec-out screencap -p > shot.png`), save to `docs/img/`, and put the first three side by side in a table at the top of this README.

1. **`tempo-result.png`** — the Tempo screen right after `stopAndAnalyze()`, showing a real set: rep count, average rep seconds, degradation percentage, failure-proximity score, and the confidence value. Do an actual working set first so the numbers are real ones. This is the screen that makes the app worth looking at.
2. **`voice-log.png`** — the Log screen mid-dictation, with the live transcript visible and the parsed fields populated underneath (exercise, weight, reps). Capture it at the moment both the raw speech and the structured result are on screen together, because that side-by-side is the whole feature.
3. **`home.png`** — the Home screen with the calendar strip and at least two weeks of logged sessions, so it does not look like a fresh install.
4. **`progress.png`** — the exercise history view for one lift with enough sessions to draw a real trend.
5. **`tempo-demo.mp4`** — optional but strong: 15 seconds of the phone in a pocket or armband through three or four reps, then the analysis appearing. Shows that the sensor path actually works on a body and not just in a hand.

Do not screenshot the Social screen. It is a placeholder and putting it in the README would misrepresent the app.
