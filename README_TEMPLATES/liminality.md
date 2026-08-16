# LIMINALITY

*a rave is buried somewhere in these walls*

A first-person liminal-space puzzle game. You wander an empty, humming corridor, the kind of hallway that should not exist at 3am, and tune the resonators hidden in its alcoves. Every locked resonator adds a layer to a techno track building behind the walls, until the third one triggers the blackout, and then the drop, when the dead corridor remembers being alive.

**Vertical slice v0.1.** One corridor, three resonators, one drop. About 1,450 lines of JavaScript, three.js, and the Web Audio API. No assets of any kind.

---

## Run it

```
npm install
npm run dev
```

Open http://localhost:5173, click, headphones on. Headphones are not a suggestion; the entire puzzle is an interference pattern between two tones.

## Controls

| Input | Action |
|---|---|
| WASD / arrows | move |
| Mouse | look (pointer lock, with a drag-to-look fallback if the browser denies it) |
| Q / E, or mouse wheel | tune the resonator you are facing |
| Esc | release the mouse |

---

## What makes it interesting technically

### The puzzle is real acoustics, not a difficulty meter

Each resonator hums at a target pitch. Approach one and your own detuned tone joins it. Two sine waves close in frequency physically beat against each other, and the beat frequency is exactly the difference between them in hertz. So the wobble you hear is not an effect layered on top of a hidden "how close am I" value. The wobble **is** the value.

The visuals are wired to the same number rather than to a separate progress variable:

```js
const detune = it.freq - it.target;
const beatHz = Math.max(Math.abs(detune), 0.4);
it.pulsePhase += dt * beatHz * Math.PI * 2;      // ring pulses at the beat rate
it.ringTune.scale.setScalar(1 + (detune / it.target) * 2.6);
```

The ring pulses at the audible wobble rate. Its size mirrors your detune, signed, so overshooting looks different from undershooting. Eyes and ears cannot disagree, because they are reading the same quantity. Detune is clamped to ±45 Hz so you can never wander somewhere the beat becomes inaudible, and the three resonators start at +27, -31, and +19 Hz so no two feel the same on approach.

### Timing lives on the audio clock

The track is sequenced by a lookahead scheduler. A `setInterval` poll wakes up periodically, looks at `AudioContext.currentTime`, and schedules every 16th note that falls inside the next ~140 ms window directly onto the WebAudio clock at 126 BPM.

That indirection matters. `setInterval` jitters, and the render loop's frame budget varies with what the GPU is doing. Neither is allowed anywhere near note timing. The scheduler's own jitter only has to be smaller than the lookahead window; the notes themselves land on a sample-accurate hardware clock. This is why the beat does not stutter when the drop fires and the post-processing stack suddenly costs more.

### The state machine reads the music, not a timer

`fx.js` has three phases: `explore` (dead fluorescent white, occasional flicker, dread), `blackout` (the breakdown, lights die while the riser climbs), and `rave` (every light becomes a colour instrument).

The phase is derived from `audio.breakdownTime` and `audio.dropTime`, which are timestamps on the audio clock, compared against `audio.now()`. There is no game timer running in parallel. The visuals cannot drift out of sync with the music, because they are not tracking the music, they are reading it. Transitions are then smoothed with frame-rate-independent exponential lerps at different rates per direction, so the rave ramps in fast (3.0) and decays slow (0.8), and the blackout kills the lights fast (2.5) but restores them faster (8.0).

### The visuals listen back

An FFT analyser splits the live output into bass, mid, and high bands, and those bands drive light intensity, emissive strength on the fixture meshes, fog colour and density, the dancefloor grid shader, bloom, and the camera's field of view. Each fixture cycles hue at its own phase offset so the corridor does not strobe as one flat unit.

The loop closes: you tune the resonators, the resonators add layers to the track, the track drives the lights, the lights are how the corridor tells you what you did.

### There are no assets

Not "few assets". None. There is no texture file, no audio file, no model, no font beyond the system stack.

Every surface (carpet, wall grime, ceiling tiles, the accent stripe) is painted procedurally onto a `<canvas>` at boot and uploaded as a texture. Every sound (the fluorescent hum, the beating drones, kick, bass, hats, claps, supersaw pads, riser, chime) is built from raw oscillators and noise buffers through the Web Audio graph. The build is a few hundred kilobytes of JavaScript and nothing else, and it loads instantly because there is nothing to load.

### Pointer lock, with a fallback that is not a dead end

Pointer lock gets denied more often than people expect: iframes, permissions policies, some embedded browsers, a user who hit Esc at the wrong moment. `player.js` falls back to drag-to-look, so the game is playable in every one of those cases instead of showing a "click to play" button that does nothing.

---

## Structure

```
src/
  main.js        boot, UI glue, render loop            (135 lines)
  audio.js       synth engine, step sequencer, FFT     (499 lines)
  world.js       corridor geometry, canvas textures,
                 fixtures, particles                   (338 lines)
  player.js      pointer-lock controller + collision    (88 lines)
  resonators.js  the tuning puzzle, real beat physics  (188 lines)
  fx.js          post-processing + phase state machine (197 lines)
```

Built with three.js 0.178 and Vite 6. Runs on anything with WebGL and Web Audio. No dependencies beyond those two.

---

## Honest state

This is a **vertical slice**, and the scope is exactly what is on the tin:

- One corridor. There is no second level, no hub, no progression between sessions.
- Three resonators, one drop, one ending. Once the drop fires, the game does not go anywhere new.
- No save state, no settings menu, no volume control beyond your system volume.
- No mobile support. It assumes a keyboard, a mouse, and pointer lock.
- Collision is simple axis-aligned corridor bounds, not a general collision system.

What is here is finished and works. Everything above is scope, not a bug list.

---

## Screenshots

TODO. This one is audio-first, so a silent GIF undersells it badly. Capture with sound.

1. **`docs/img/tune.mp4`** — the most important capture, and it must have audio. Approach a resonator, tune it in over about 15 seconds, and let the mic or system capture pick up the beat frequency slowing as the ring pulse slows with it. A viewer should be able to hear the wobble and watch the ring, and understand without narration that they are the same signal. Put this at the top.
2. **`docs/img/drop.mp4`** — 20 seconds spanning the third lock, the blackout, and the drop. Also with audio. This is the payoff shot: white corridor, lights dying, then every fixture becoming a colour instrument on the FFT.
3. **`docs/img/corridor.png`** — a still of the `explore` phase, framed down the corridor with an alcove visible on one side. This is the "liminal space" image and it should look genuinely uncomfortable. Take it at 1920x1080.
4. **`docs/img/rave.png`** — the same corridor mid-drop for the before-and-after pairing. Same camera position as the shot above if you can manage it, because identical framing makes the contrast do the work.

If you only produce one artifact, make it the tuning clip with audio. The puzzle is unexplainable in text and obvious in five seconds of sound.
