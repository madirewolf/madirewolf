<!-- BEGIN:header -->
# Mohammad Abu Daqer

Founding Engineer at Vimy Systèmes. Cinematic WebGL, full-stack product, applied AI.

Toronto, Ontario, Canada
<!-- END:header -->

I build the visible layer of hard systems. That is usually three things at
once: an interface with real craft in it (Three.js, WebGL, GLSL I write by
hand, motion that carries meaning instead of decorating it), a machine
underneath that holds up (React and Next.js over Python, FastAPI and Postgres,
with the unglamorous parts actually done: migrations, cost logging, tests), and
AI in the loop where it earns its place (retrieval pipelines, the Anthropic
API, pgvector, and a human who still has to click send).

I came to it through hardware. Fourteen months at Tenstorrent qualifying
Wormhole and Grayskull silicon, a computer engineering degree at the University
of Toronto, then first engineering hire at Vimy Systèmes building autonomy and
explainable AI for aerospace and defence. The habit stuck: ship it, measure it,
say only what is true about it. Every number below is measured. Where a number
is missing, it was never measured, and it is left blank rather than guessed.

<!-- BEGIN:selected-work -->
## Selected work

| Project | What it is | Stack | Measured |
| --- | --- | --- | --- |
| [limiliminal](https://limiliminal.com) ([code](https://github.com/madirewolf/my_universe)) | Interactive 3D solar-system portfolio in Three.js with custom GLSL planet shaders | TypeScript, GLSL, Three.js | 5,434 lines TypeScript · 23 files |
| [JobFinder](https://github.com/madirewolf/JobFinder) | LLM job-discovery pipeline: 6 ATS feeds, pgvector RAG, Claude triage and drafting | Python 3.12, Anthropic API, Claude Haiku | 7,941 lines Python · 76 files |
| [LIMINALITY](https://github.com/madirewolf/liminality) | First-person liminal-space puzzle game where tuning resonators unburies a techno track | JavaScript, Three.js, WebGL | not measured |
| [complere](https://github.com/madirewolf/complere) | Editorial supplements storefront with hand-drawn SVG molecules and a full checkout flow | TypeScript, Next.js 15, React 19 | 3,816 lines TypeScript · 33 files |
| [Redline](https://github.com/madirewolf/Redline) | Voice-controlled Android fitness tracker: hands-free logging, calories, tempo analysis | Kotlin, Jetpack Compose, Material 3 | 4,558 lines Kotlin · 46 files |
| [Reel Block](https://github.com/madirewolf/Reel_block) | Android accessibility service that blocks the Reels feed but allows DM-shared reels | Kotlin, Android Accessibility Service, AndroidX | 2,298 lines Kotlin · 15 files |
| [ECHONOMY](https://echonomy.limiliminal.com) ([code](https://github.com/madirewolf/echonomy)) | Spotify sample archaeology — meet the originals hiding inside your favourite tracks | JavaScript, Node.js, Express | not measured |
| [ourspace](https://github.com/madirewolf/ourspace) | Portfolio-first social platform with low-poly avatars and walkable creator galleries | TypeScript, Three.js, React | not measured |

Course work, contributor work, and the closed-source projects are in [projects.json](https://github.com/madirewolf/madirewolf/blob/main/projects.json) with the rest of the dataset.
<!-- END:selected-work -->

<!-- BEGIN:restricted-work -->
## Restricted work

Client and program work for Vimy Systèmes and Canadian DND IDEaS competitions. Real systems, measured the same way as everything above, with no repository to open.

**FinalFusion — Maritime Domain Awareness** · lead engineer · 2026

- Multi-modal sensor-fusion platform for maritime domain awareness (DND IDEaS CFP6)
- 24,300 lines Python · 395 files · 88 commits · 54 test files
- Architected a multi-modal sensor-fusion platform as a 23-package pnpm + uv monorepo: 10 FastAPI microservices, 9 shared Python libraries, and 4 TypeScript packages, cleanly split between libs and services.
- Restricted: Vimy Systèmes / DND IDEaS CFP6 Challenge 13 — client IP, pre-submission. Architecture and metrics only; no repository access.

**GPS-Denied Navigation Module** · lead engineer · 2026

- Flight-controller-agnostic GNSS-denied localization for small UAVs
- 7,666 lines Python · 73 files · 26 commits · 18 test files
- Built a GNSS-denied localization pipeline (~7,700 lines of Python across a documented six-layer architecture) fusing an error-state EKF, visual-inertial odometry, and pose-graph SLAM into a single honest-covariance pose estimate.
- Restricted: Vimy Systèmes product R&D — company IP, productisation in progress. Architecture and metrics only; no repository access.

**5GCx — Pilot AI Evaluation** · contributor · 2025-2026

- Gaze and head-pose analysis pipeline for pilot readiness and HMI evaluation
- Live: [5gcx.ai](https://5gcx.ai)
- Restricted: Vimy Systèmes partner-organisation contract — implementation specifics governed by partner NDA. Public framing only; no repository access.

**AMMVER — Emerging-Threat Forecasting** · lead engineer · 2025-2026

- Explainable conflict forecasting with a hybrid neural-network and hidden-Markov model
- Restricted: Vimy Systèmes / DND IDEaS Fast Forward program — client IP. Concept and role only; no repository or model access.

**Drone Surge — Cardboard UAS Concept** · co-author · 2025-2026

- Round 2 design package for DND's attritable-UAS competition after a $35,000 Round 1 award
- Restricted: Vimy Systèmes / DND IDEaS Drone Surge competition — client IP and live competition material.

**vimy.ai — Public Web Presence** · contributor · 2025-2026

- Public product surface for a deep-tech defence firm: React 19, motion, 3D embeds
- Live: [vimy.ai](https://vimy.ai)
- Restricted: Vimy Systèmes company property — the live site is public, the source is not.
<!-- END:restricted-work -->

<!-- BEGIN:currently -->
## Currently

Actively looking: Full Stack Engineer, Front End Engineer, Design Engineer, Creative Developer, AI Engineer. Full-time, contract, freelance. Remote or Toronto, Canada.

[mohammad.abu.daqer@gmail.com](mailto:mohammad.abu.daqer@gmail.com) · [limiliminal.com](https://limiliminal.com) · [LinkedIn](https://www.linkedin.com/in/mohammad-abu-daqer/)
<!-- END:currently -->

---

<!-- BEGIN:footer -->
This page is generated from [projects.json](https://github.com/madirewolf/madirewolf/blob/main/projects.json) by [build.py](https://github.com/madirewolf/madirewolf/blob/main/build.py). Machine readable: [api.json](https://raw.githubusercontent.com/madirewolf/madirewolf/main/api.json), [resume.json](https://raw.githubusercontent.com/madirewolf/madirewolf/main/resume.json) (JSON Resume v1.0.0).

AI agents: start at [llms.txt](https://raw.githubusercontent.com/madirewolf/madirewolf/main/llms.txt).
<!-- END:footer -->
