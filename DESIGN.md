# Design

The page is a passport: a bordeaux cover with gold foil, sage-paper visa pages, and an ink stamp for every anomalous fare. Visitor mode: Operate. Rank comes from weight, case and reversal, not from many sizes. Hairlines replace boxes.

## Color

| Token | Value | Use |
|---|---|---|
| `--cover` / `--cover-deep` | #5A1A2B / #431220 | Header band; mark for ponti and holidays in the strips |
| `--foil` / `--foil-soft` | #E2C48A / #D9C3A0 | Text, chip and guilloché on the cover |
| `--paper` / `--paper-2` | #EEF1E8 / #E3E8DA | Page ground / row hover |
| `--ink` / `--ink-2` | #1E2421 / #4C5750 | Text / secondary text (≥ 4.5:1 on paper) |
| `--rule` | #C3CABB | 1px hairlines between rows |
| `--violet` | #47399A | Anomaly (stamp, price, bar), focus, active toggles |
| `--green` | #1D6743 | Anomaly flagged today |
| `--red` | #A3222E | Errors |

## Type

- **Bodoni Moda** (self-hosted, variable, optical size): cover title in caps tracked at 0.04em, and the period dates.
- **Red Hat Mono** (self-hosted): airport codes, times, prices, and the small-caps labels.
- **System sans**: names and running text.
- Four sizes only: 0.8125rem, 1rem, 1.375rem, and `clamp(2rem, …, 3.25rem)`. Numbers are tabular.

## Components

- **Period page**: a large Bodoni date range, a mono caps line below it (days and kind of period), the lowest price on the right, then a 1px ink rule and the fare rows.
- **Fare row**: destination | route and times | price + "tipico" | stamp | "Prenota" button with a 1px border that inverts on hover. On narrow screens it becomes a two-column grid.
- **Stamp**: 2px border in ink color, rotated a few degrees (stable per fare), with the SVG `#ink` filter (turbulence + displacement) and `multiply` blending. It lands once on first paint (scale 1.35 to 1 with a blur that clears, 520ms ease-out); there is no motion with reduced-motion.
- **Year strip** (Mete view): one bar per period, height proportional to price. Grey 30% for normal fares, violet or green for anomalies, a 3px bordeaux base for ponti and holidays, a 1px line when there is no fare. Bars are links with a tooltip and an aria-label.
- **Controls**: a segmented tab with ink inversion, pill toggles that turn violet when on, a search field with a violet caret, and a budget slider. They are sticky on desktop and static on mobile.

## Spacing

`--gutter: clamp(1rem, …, 2.5rem)`, max width 76rem, rows 0.7rem vertical padding, more space above a period than below it.
