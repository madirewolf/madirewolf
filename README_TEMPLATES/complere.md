# complere

An editorial supplements storefront, built solo from a design brief. The product images are hand-drawn skeletal formulas with correct chemistry, not stock photography.

Next.js 15, React 19, Tailwind v4, Zustand. Roughly 3,800 lines of TypeScript across 33 files, 7 routes, statically exported.

---

## What it is

A complete storefront: home, product index, product detail, a long scroll-driven Story page, cart, checkout, and a success page. Museum-card aesthetic, one accent colour used in exactly six places, no drop shadows heavier than `shadow-sm`.

The brief said mock the checkout. It is mocked. There is no payments backend and this README is not going to imply there is one.

Every non-obvious decision, including the ones I got wrong first, is written down in [`DECISIONS.md`](./DECISIONS.md).

---

## What makes it interesting technically

### The section indicator does not use IntersectionObserver

The Story page has a fixed indicator showing which section you are in. The obvious tool is `IntersectionObserver`, and it was the first implementation, and it was unreliable: on a fast flick scroll or a programmatic jump, a section boundary can cross the entire threshold band inside a single frame and the callback simply does not fire for it. The indicator would silently stick on the wrong section.

The replacement (`components/section-indicator.tsx`) is a `requestAnimationFrame`-driven scroll listener. On every frame it measures each on-screen section's centre and picks whichever is closest to 40% of the viewport height. It is throttled to one rAF in flight at a time, the scroll listener is `passive`, and it re-measures on resize. It cannot skip a boundary because it never depends on an event firing at the right moment; it just answers "where am I" every frame from live geometry.

### The hydration flash

`persist`-backed Zustand rehydrates from `localStorage` after first paint. Load `/cart` or `/checkout` directly and you get one frame of the empty-cart state before the items arrive, which looks like a bug at exactly the moment a user is most nervous.

The store wires `onRehydrateStorage` to a `hasHydrated` flag, and the cart and checkout routes render nothing decisive until it flips. `partialize` persists only `items`, so transient UI state like the drawer's open flag never survives a reload and cannot resurrect a drawer on page load.

### Six molecules, drawn by hand, chemically correct

`components/molecule-structure.tsx` is 409 lines of hand-tuned SVG:

| Product | Structure |
|---|---|
| Vitamin D3 + K2 | cholecalciferol secosteroid |
| Magnesium Glycinate | Mg²⁺ chelate with two glycinate ligands |
| Methylated B-Complex | pteridine ring with N5-methyl, plus a stub for the PABA-glutamate tail |
| Omega-3 (EPA/DHA) | 20-carbon EPA chain with all five cis double bonds drawn explicitly |
| Creatine Monohydrate | guanidino-methylglycine |
| L-Theanine | γ-glutamyl ethylamide |

The molecule comparison on the Story page mirrors the R-side via `scaleX(-1)` to show the chiral carbon attachment, which is the correct way to draw an enantiomer and takes one CSS transform instead of a second hand-drawn structure.

Molecular formulas in the captions do not auto-uppercase. Tailwind's `uppercase` would turn `C₄H₈MgN₂O₄` into `C₄H₈MGN₂O₄`, which is wrong, because chemical symbols preserve case. The mono caption keeps mixed case with slightly tightened tracking.

There is no draw-in animation on the structures, and the reason is a real limitation rather than taste: Motion's `whileInView` and `useInView` both behave badly on SVG `<line>` elements, which have zero bounding-box area, so `pathLength: 0 → 1` animations stuck mid-flight. The static line drawing reads better anyway.

### Reduced motion is a render branch, not a duration of zero

The `Reveal` component checks the OS preference and, when it is set, renders its children with **no motion props at all** rather than with a zero-duration animation. Nothing is handed to the animation library, so there is no library overhead and no chance of a half-applied transform. Stat counters skip the count animation and jump to the final value. A CSS-level `prefers-reduced-motion` override in `globals.css` is a second layer that catches anything not routed through the React hook.

### Small things that were still decisions

- **Card input masking is hand-rolled.** Card number splits into 4-digit groups, expiry inserts `MM / YY`, CVC is digit-only. About 15 lines. Not worth a dependency and its transitive tree.
- **The order number goes into `sessionStorage`, not route state.** Refreshing `/checkout/success` keeps the number instead of losing it to a route-state cache.
- **The bioavailability table has two distinct layouts** at the `md` breakpoint: a publication-style 5-column grid on desktop, per-row definition lists on mobile. Forcing one layout into both produced a wall of mashed text.
- **Next.js was bumped off 15.1.3** to a patched 15.5.x line after npm flagged a CVE, before anything shipped.

---

## Stack

TypeScript, Next.js 15 App Router, React 19, Tailwind CSS v4 with the CSS-first `@theme` approach, `motion/react`, Zustand 5 with `persist`, Radix primitives for dialog and popover, Lucide icons. `output: "export"`, so `npm run build` produces a static `out/` directory you can host anywhere.

Fonts: Instrument Serif for the wordmark and display type, set italic. There is no logo asset; the wordmark is the typeface.

---

## Run it

```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # static export to ./out
```

No environment variables. No database. No API keys. Nothing to stand up.

---

## Layout

```
app/
  page.tsx                    home
  products/page.tsx           index
  products/[slug]/            detail + product-detail.tsx
  story/page.tsx              the long scroll-driven editorial page
  cart/page.tsx
  checkout/page.tsx
  checkout/success/page.tsx
components/
  molecule-structure.tsx      6 hand-drawn skeletal formulas
  molecule-comparison.tsx     chiral carbon, mirrored via scaleX(-1)
  section-indicator.tsx       rAF scroll tracking
  reading-progress.tsx        useScroll + sprung scaleY
  bioavailability-table.tsx   two layouts, one dataset
  reveal.tsx                  reduced-motion-aware reveal
  cart-drawer.tsx  product-card.tsx  citation.tsx  stat-block.tsx  nav.tsx  footer.tsx
lib/
  data/products.ts            6 products
  data/citations.ts
  store/cart.ts               Zustand + persist + hasHydrated
  hooks/                      use-count-up, use-reduced-motion
DECISIONS.md                  why everything above is the way it is
```

---

## Honest state

This is a **demo built from a brief**, and it is complete as a demo. What that means specifically:

- Checkout is mocked. No Stripe, no payment processing, no order persistence beyond `sessionStorage`.
- Product imagery is typographic placeholders plus the SVG structures. There is no product photography.
- No backend, no email send, no lab-certificate PDFs, no newsletter (the brief explicitly said to kill the newsletter).
- Six products, hard-coded in `lib/data/products.ts`. There is no CMS and no inventory model.

Everything that is here works: the cart persists, the checkout flow completes, reduced motion is respected end to end, and the static export deploys.

---

## Screenshots

TODO. Save to `docs/img/` and link at the top.

1. **`home.png`** — full-page capture of `/` at 1440px. The editorial hierarchy is the pitch, so capture the whole page rather than the fold.
2. **`product-detail.png`** — `/products/omega-3` specifically. It has the most visually complex structure (20-carbon chain, five explicit cis double bonds) and it is the clearest proof the molecules are drawn rather than sourced. Frame it so the structure and the museum-card caption underneath are both fully readable.
3. **`molecules.png`** — a contact sheet of all six product cards in the grid, so the reader sees six distinct structures in one glance.
4. **`story-scroll.gif`** — a 10 to 12 second scroll through `/story` at 1440px, capturing the section indicator changing on the right and the reading-progress line filling on the left. That is the one interaction a still cannot show.
5. **`checkout.png`** — `/checkout` with the card fields partially filled, so the hand-rolled masking is visible mid-entry (`4242 4242 42` and `12 / 2`).

If you also want a mobile shot, `/story` at 390px is the interesting one, because the bioavailability table switches to a completely different layout there.
