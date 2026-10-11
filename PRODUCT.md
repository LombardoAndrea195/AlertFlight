# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Static HTML/CSS/JS with no build step, published on GitHub Pages by the daily GitHub Action. The page reads `data.json`, which the Python job (`voli_alert`) writes on every run.

## Users

One person (the owner), based in Rome. They open the page to decide where to go for an upcoming weekend or long weekend (ponte), and then book through the airline or agency link.

## Product Purpose

Every day the job checks all weekends and ponti for the next 12 months, finds the cheapest return flight from Rome (FCO + CIA) to every destination, and flags fares that are unusually cheap compared with that destination's typical price. Telegram pushes the new anomalies; the page lets the owner browse everything found that day. Success means spotting a good, bookable trip in seconds.

## Positioning

Unlike Google Flights or Skyscanner "Everywhere", it is organised around the owner's real free time (weekends and Italian/Roman public holidays plus bridge days). It only proposes trips that fit (leave the evening before after 17:00, return on the last day after 12:00) and judges price against the route's own history instead of a fixed budget.

## Operating Context

- Daily cron on GitHub Actions; Telegram alert for new anomalies; the web page for browsing.
- Sources: Ryanair fare finder (live, direct flights) and Travelpayouts/Aviasales (cached data from the last 48 h, all airlines, worldwide; optional token).
- Prices are round trip, in EUR, for 1 adult. They can change by booking time.

## Capabilities and Constraints

- Periods: weekends + holidays from `holidays` (IT, subdiv RM); one leave day bridges a Tuesday/Thursday holiday.
- "Anomaly" = at least 50% below the destination's typical price (the median of daily medians over 90 days) and at least €15 saved. Configurable in `config.toml`.
- Public GitHub Pages site and public repository; secrets stay in Actions secrets.
- Italian UI.

## Evidence on Hand

Real fare data produced by the job (`site/data.json`). No testimonials, brand, or logo exist; do not invent them.

## Product Principles

1. Free time first: the period is the primary axis, destination second.
2. Show only trips you could actually take.
3. Show price together with context ("instead of ~X €"), never as a bare number.
4. One click from seeing a fare to booking it.
