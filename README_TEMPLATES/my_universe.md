# limiliminal

An interactive 3D portfolio you fly through instead of scroll. Two universes, a planet per discipline, a moon per project, custom GLSL for every planet type, and a transition that keeps animating while the WebGL thread is completely stalled.

Live at **[limiliminal.com](https://limiliminal.com)**.

Next.js 14 App Router, React 18, TypeScript, Three.js via React Three Fiber. 25 TypeScript files, 12 of which are 3D components, plus about 1,300 lines of hand-written GLSL.

---

## What makes it interesting technically

### The transition problem, and the fix that is not WebGL

Switching universes means swapping the entire scene tree. That means shader compile and link on first draw, React committing the new tree, and buffer uploads, all on the main thread, all at once. The first version drew the warp corridor inside the Canvas, so the corridor was rendered by the exact thread that was stalling. The streaks visibly froze mid-flight, on every device.

`components/rift-warp-overlay.tsx` is the answer: the warp is DOM and CSS, not WebGL. It animates only `transform` and `opacity` on composited layers, which browsers run on the **compositor thread**. The loop keeps spinning while the main thread is blocked solid. The overlay's base is a radial gradient tuned to match what the rift core looks like when it engulfs the camera, so both handoffs (fade in as the camera dives into the source rift, fade out with the camera already pinned inside the destination rift) read as one continuous shot rather than a cut to a loading screen.

The same trick shows up in `boot-screen.tsx`, where the loading dots animate `opacity` and the sweep bar animates `transform` rather than `left`, for exactly the same reason.

### Waiting for shaders honestly

Hiding a stall behind an overlay is only half of it. `RiftCompileGate` uses `WebGLRenderer.compileAsync` (three r152+, backed by `KHR_parallel_shader_compile`) to verify every material in the incoming scene tree is compiled and linked before the cinematic is allowed to release. The `peak` stage holds until that promise resolves, with a `PEAK_MIN_DURATION` floor of 0.5s so a warm cache does not produce a jarringly short transition, and a completed-frame check as the backstop. There is a documented fallback path for browsers without `compileAsync`.

### The pole singularity

The system view settles the camera at `(0, 52, 0.6)`, not `(0, 52, 0)`.

Looking straight down at the origin from directly overhead makes the forward vector parallel to the up vector. That is the one orientation `lookAt` cannot define, so roll collapsed to an arbitrary fallback in the final frames of the return flight, and the whole scene visibly snapped to a new rotation right as it settled. The 0.6 offset on z keeps the camera off the exact pole and the degeneracy never happens.

Two more pieces of the same fix:

- `CAMERA_FLIGHT.GAZE_LEAD = 0.6`. The gaze pan finishes recentering in the first 60% of the flight, then the dolly finishes the framing. Sharing one clock made the look-at *linger* near the degenerate orientation early in a dive from overhead, which read as jitter. Leading the gaze sweeps out of that zone fast.
- `syncCameraLookAt()` builds orientation from a matrix `lookAt` with a **pinned** up vector captured at flight start, so roll never twists through a move. It also deliberately does not call `controls.update()` mid-flight, because `update()` enforces the destination mode's min/max distance and polar clamps, and mid-transition the camera is legitimately outside them (52 units out while planet mode allows 22). Calling it snapped the camera to the clamp while the lerp pulled it back. Controls stay disabled during scripted moves and get one clean `update()` at handoff.

### Seam-free planet shaders

`lib/shaders.tsx` is about 1,300 lines of GLSL across 13 planet types, keyed by `PlanetEntry.type`. Every one of them samples noise in **object space** (`normalize(vObj)`) rather than from UVs. A sphere's UV parameterisation has a wrap seam down one meridian and pinches at both poles; 3D noise sampled against the object-space normal has neither, and it stays anchored to the body so the pattern rotates with the planet instead of swimming across it. The voronoi shells, the circuit traces, the caplet split, and the knot-wrapped types all rely on it.

### Two clocks

Pausing the scene should freeze orbital motion without freezing the surface of a star. So `planet.tsx`, `sun.tsx`, and `rift.tsx` all run a dual-clock pattern: an accumulated `effectiveTime` that stops advancing while paused, and raw `state.clock.elapsedTime` for anything that should stay alive.

Orbit revolution, moon spin, and gross rotation run on effective time and hold exactly where they were. Surface boil, corona, chromosphere flicker, and hover pulses run on wall time, so a paused sun still churns. The wall-clock bookkeeping keeps running while paused so that nothing jumps forward by the pause duration on resume.

### The match cut

Mode transitions (system to planet to moon) use a camera-only state machine, `MODE_CUT`, with no overlay at all.

Big to small: pan onto the clicked object while tracking it live as it orbits, dive until it fills the frame (`FILL_FACTOR` 1.9 times its bounding radius), swap the scene *behind the object itself*, then ease back out to the settle distance. Planet filling the frame cuts to detail planet filling the frame. Because the moon crystal is spiky, its bounding-sphere fill distance leaves too much background showing, so crystal cuts dive to a hand-tuned 2.1 units instead, just outside the displaced spike envelope so the near plane never clips into it. A symmetric FOV punch-in held across the cut covers the last stretch with the lens rather than the dolly.

Small to big: one continuous zoom-out with the cut placed mid-zoom, arriving already moving outward. The persistent starfield and nebula carry the cut.

There is no opacity fade anywhere in this path, and the code says why: the sun, planet, and rift materials are custom shaders that ignore `material.opacity`, so a "fade" leaves them fully visible. The system hides and shows with a plain visibility flip, always masked by the cut.

### The scene has a text mirror

A WebGL canvas is invisible to crawlers and to screen readers. `app/page.tsx` server-renders an `sr-only` section that walks the same `UNIVERSE_CONFIG` data structure and emits every planet, landmark, description, technology list, and link as plain semantic HTML. Same content, same source of truth, no duplication.

---

## Stack

Next.js 14 (App Router), React 18, TypeScript in strict mode, Three.js 0.168, @react-three/fiber, @react-three/drei, Tailwind CSS, Radix UI primitives. Shaders are raw GLSL strings compiled into `THREE.ShaderMaterial`, no shader library. Deployed to Vercel and Netlify on push.

---

## Run it

```bash
npm install
npm run dev     # http://localhost:3000
npm run build
```

No environment variables, no API keys, no backend. It is a static-ish Next app that renders a canvas.

---

## Layout

```
app/
  page.tsx                  entry + the sr-only accessible mirror
  layout.tsx
components/
  solar-portfolio.tsx       the big one: CameraController, MODE_CUT, rift stage machine
  rift-warp-overlay.tsx     DOM/CSS warp + RIFT_TIMING (shared timing source of truth)
  planet.tsx                planet + moon rendering, dual-clock pause
  sun.tsx  rift.tsx  moon-view.tsx  star-field.tsx  star-nest.tsx
  nebula.tsx  cosmic-dust.tsx  background.tsx
  ui-overlay.tsx  welcome-intro.tsx  boot-screen.tsx
  space-ambience.tsx  background-music.tsx
lib/
  shaders.tsx               13 GLSL planet materials, ~1,300 lines
  constants.ts              UNIVERSE_CONFIG: content + render config
VOICE.md                    canonical source for the personal-universe copy
```

---

## Honest state

- **Shipped and live.** It is the portfolio, not a demo.
- **It started from a v0.app scaffold.** The initial layout came out of v0; almost all of the Three.js, all of the GLSL, the camera state machines, and the scene architecture are hand-written on top of it. The repo history shows that split honestly and this README is not going to pretend otherwise.
- **`lib/constants.ts` still welds content to render config.** Names, descriptions, and technology lists live in the same objects as orbital distance, speed, tilt, and shader key. That is a known defect. The plan is to split it: content moves to a `projects.json` keyed by stable ids, presentation moves to a `presentation.ts` mapping id to orbital config, and `constants.ts` becomes the join. Nothing in the render path assumes the current shape beyond the `PlanetEntry` and `Landmark` types.
- **`VOICE.md` is upstream of `constants.ts`** for anything on the personal universe. Edit it first, then propagate.

---

## Screenshots

TODO. Static images undersell this one badly. Priority order:

1. **`docs/img/rift.gif` or `.mp4`** — the single most important capture. Record the full rift cinematic from the professional universe to the personal one: click the rift, ride the dolly in, through the warp, and all the way out to the overhead settle. About 12 seconds. Record at 60fps if your capture tool allows it, because the whole point of the transition is that it does not drop frames. This belongs at the very top of the README.
2. **`docs/img/system.png`** — the overhead system view, 1920x1080, taken a few seconds after load so the planets have spread out around their orbits rather than clustering at phase zero.
3. **`docs/img/planets.png`** — a contact sheet of four or five different planet types side by side at close range, chosen to show that the shaders are genuinely different from each other (the voronoi one, the circuit-trace one, the knot, the caplet). Zoom in close enough that a reader can see there is no seam.
4. **`docs/img/moon-view.png`** — the moon detail view with a project's content panel open, so it is obvious the scene is a real navigation surface and not just a pretty background.

If you only make one, make the rift recording.
