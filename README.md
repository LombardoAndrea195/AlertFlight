# Voli economici da Roma per weekend e ponti

Ogni mattina controlla **tutti i weekend e i ponti dei prossimi 12 mesi**, cerca i voli andata e ritorno da Roma (FCO + CIA) verso qualsiasi destinazione e:

- ti manda su **Telegram** i nuovi voli con un **prezzo anomalo** (almeno il 50% sotto il prezzo tipico di quella meta);
- pubblica una **pagina web** su GitHub Pages con tutte le tariffe del giorno.

## La pagina

- **Periodi**: ogni weekend o ponte, con la data e le mete più economiche. I prezzi anomali hanno un timbro (verde se trovati oggi, viola se già segnalati).
- **Mete**: per ogni destinazione, una barra per ogni periodo dell'anno (più alta = più cara). Così vedi subito quando conviene andarci. Tocca una barra per prenotare.
- **Filtri**: solo prezzi anomali, solo ponti e feste, solo voli diretti, budget massimo, ricerca per meta o paese. Vengono ricordati dal browser.

È HTML/CSS/JS statico in `site/` e non ha bisogno di build. I dati sono in `site/data.json`, generato a ogni esecuzione.

## Come funziona la ricerca

1. **Periodi**: weekend e ponti calcolati dalle festività italiane (+ Santi Pietro e Paolo per Roma). Se una festa cade di martedì o giovedì, aggiunge un giorno di ferie per fare il ponte.
2. **Ricerca "ovunque"**: per ogni periodo una sola richiesta restituisce il prezzo più basso verso *tutte* le destinazioni:
   - **Ryanair** (gratis, prezzi in tempo reale, voli diretti in Europa e Mediterraneo)
   - **Travelpayouts / Aviasales** (gratis con token, tutte le compagnie e tutto il mondo, dati in cache delle ultime 48 ore)
3. **Orari sensati**: la sera prima si parte dopo le 17, il primo giorno libero entro le 14, e l'ultimo giorno si rientra dopo le 12.
4. **Prezzo anomalo**: il prezzo tipico di ogni meta è la mediana dei prezzi su tutti i periodi negli ultimi 90 giorni. È anomalo se costa almeno il 50% in meno e fa risparmiare almeno 15 €.
5. **Niente doppioni** su Telegram: lo stesso volo viene segnalato di nuovo solo se scende di un altro 10%.

Lo storico è in `data/state.json` (pochi KB) e viene salvato con un commit giornaliero. Soglie e orari sono in `config.toml`.

## Pubblicazione su GitHub (una sola volta)

1. **Repository**: crea un repository **pubblico** su GitHub (con un account gratuito, Pages funziona solo sui repository pubblici) e carica questa cartella:
   ```sh
   git init -b main
   git add .
   git commit -m "Alert voli economici"
   git remote add origin git@github.com:<utente>/<repo>.git
   git push -u origin main
   ```
2. **Pages**: *Settings → Pages → Build and deployment → Source: GitHub Actions*.
3. **Secret**: *Settings → Secrets and variables → Actions → New repository secret*:
   - `TELEGRAM_BOT_TOKEN`: scrivi a [@BotFather](https://t.me/BotFather), `/newbot`, copia il token.
   - `TELEGRAM_CHAT_ID`: manda un messaggio al tuo bot e apri `https://api.telegram.org/bot<TOKEN>/getUpdates` per leggere `chat.id`.
   - `TRAVELPAYOUTS_TOKEN` (facoltativo, aggiunge le mete intercontinentali): registrati su [travelpayouts.com](https://www.travelpayouts.com) e copialo da *Profilo → API token*.
4. **Primo avvio**: *Actions → Voli economici → Run workflow*. Dopo un paio di minuti la pagina è online su `https://<utente>.github.io/<repo>/`.

Senza i secret Telegram il workflow funziona lo stesso: aggiorna solo la pagina.

## Il workflow (`.github/workflows/daily.yml`)

| Quando | Cosa fa |
|---|---|
| Ogni giorno alle 8:00 (7:00 d'inverno) o con *Run workflow* | Cerca i voli, invia Telegram, salva lo storico, pubblica la pagina con i nuovi dati |
| Push su `main` che cambia `site/` o il workflow | Ripubblica solo la pagina, riusando i dati già online (nessuna ricerca, nessun messaggio) |

## Uso in locale

```sh
uv run python -m voli_alert --dry-run          # cerca, scrive site/data.json, stampa il messaggio senza inviarlo
python3 -m http.server 8000 --directory site   # apri http://localhost:8000
uv run pytest
```
