# Product

## Register

product

## Users

Operators and engineers use SmartShelf equally. Operators glance at a wall-mounted or kiosk 1024×600 touchscreen for live shelf occupancy and status. Engineers use the same UI on phones, tablets, and desktop PCs to review history, catch alerts, and configure cell trigger bounds. Primary job on Live Tiles: glance occupancy/status at a distance; secondary jobs: notice alerts and open cell configuration without friction.

## Product Purpose

SmartShelf is a live ops dashboard for a 9-node sensor shelf. It streams temperature, humidity, VOC, and occupancy so users can see which cells are full, empty, offline, or alerting, inspect minute history and realtime cache charts, and set category or custom trigger bounds per cell. Success is: all nine tiles readable on a 1024×600 screen without scrolling, status understandable in under a second, and configuration reachable with large touch targets on every supported device.

## Brand Personality

Precise, industrial, calm. The interface should feel like a reliable instrument panel: quiet confidence, clear hierarchy, no spectacle. Soft nod to Stripe Dashboard clarity (clean structure, restrained chrome) without copying its look.

## Anti-references

- Generic dark SaaS with cyan glow, glassmorphism, and neon accents
- Busy Grafana-style walls of charts competing for attention on the primary view
- Consumer smart-home playful UI
- Identical marketing-card grids and hero-metric SaaS clichés

## Design Principles

1. **Glance first** — Occupancy and health must be readable in one look; charts and config stay secondary until requested.
2. **Fit the panel** — The 3×3 live tile grid must fit 1024×600 without scroll; denser layouts scale up cleanly on larger screens.
3. **Touch without clutter** — Large hit targets and clear affordances; chrome stays minimal so data stays primary.
4. **Status with meaning** — Empty, full, offline, and alert states use shape/label plus color, never color alone.
5. **Calm instrument** — Restrained surfaces, deliberate hierarchy, no decorative glow or chart overload on the home view.

## Accessibility & Inclusion

Practical readability over formal WCAG certification for now. Touch targets must be large enough for 1024×600 touchscreens. Prefer high-contrast text and status labels that remain understandable without relying on color alone. Respect reduced-motion preferences where animation is used.
