# Voli economici da Roma per weekend e ponti

Ogni mattina controlla **tutti i weekend e i ponti dei prossimi 12 mesi**, cerca i voli da Roma (FCO + CIA) verso qualsiasi destinazione, **andata e ritorno, solo andata e solo ritorno**, e:

- ti manda su **Telegram** i nuovi voli andata e ritorno con un **prezzo anomalo** (almeno il 50% sotto il prezzo tipico di quella meta);
- pubblica una **pagina web** su Render con tutte le tariffe del giorno, comoda anche da telefono.

## La pagina

- **Tipo di viaggio**: andata e ritorno, solo andata (Roma → meta) o solo ritorno (meta → Roma).
- **Periodi**: ogni weekend o ponte, con la data e le mete più economiche. I prezzi anomali hanno un timbro (verde se trovati oggi, viola se già segnalati).
- **Mete**: per ogni destinazione, una barra per ogni periodo dell'anno (più alta = più cara). Così vedi subito quando conviene andarci. Tocca una barra per prenotare.
- **Filtri**: solo prezzi anomali, solo ponti e feste, solo voli diretti, budget massimo, ricerca per meta o paese. Vengono ricordati dal browser.

È HTML/CSS/JS statico in `site/` e non ha bisogno di build. I dati sono in `site/data.json`, generato a ogni esecuzione.

## Come funziona la ricerca

1. **Periodi**: weekend e ponti calcolati dalle festività italiane (+ Santi Pietro e Paolo per Roma). Se una festa cade di martedì o giovedì, aggiunge un giorno di ferie per fare il ponte.
2. **Fonti**:
   - **Ryanair** (gratis, prezzi in tempo reale, voli diretti in Europa e Mediterraneo): una richiesta per periodo restituisce tutte le destinazioni.
   - **Wizz Air** (gratis, prezzi in tempo reale, voli diretti): una richiesta per tratta ogni 42 giorni. Wizz Air blocca chi va troppo veloce, quindi si fa una richiesta ogni 2 secondi circa: la ricerca completa dura una mezz'ora, in parallelo alle altre.
   - **Vueling** (gratis, prezzi in tempo reale, solo da Fiumicino): il volo più economico di ogni giorno, con l'orario, per le tratte dirette (Madrid, Barcellona, Ibiza, Bilbao, Tenerife, Parigi, Londra). La cache di Vueling a volte risponde con un'altra tratta: in quel caso si aspetta che scada (circa 5 minuti) e si riprova, su 4 code in parallelo.
   - **Lufthansa** (gratis, tutto il gruppo: Lufthansa, Swiss, Austrian, Brussels...): solo andata e ritorno, per le mete che Lufthansa pubblicizza da Roma. Dà il prezzo per ogni coppia di date ma **non l'orario né gli scali**.
   - **Iberia** (gratis, Roma–Madrid): solo andata e ritorno, **senza orario**. Il sito di Iberia blocca i programmi, quindi le richieste passano da `curl_cffi`, che si presenta come Chrome.
   - **Travelpayouts / Aviasales** (gratis con token): **tutte le compagnie e tutto il mondo, compresi i voli intercontinentali** e con scalo. Dati in cache delle ultime 48 ore.

   easyJet e ITA Airways non hanno un'API utilizzabile (easyJet blocca ogni richiesta, ITA mostra solo prezzi per viaggi di 14 giorni): i loro voli arrivano solo da Travelpayouts.
3. **Orari sensati**: la sera prima si parte dopo le 17, il primo giorno libero entro le 14, e l'ultimo giorno si rientra dopo le 12. Valgono anche per i voli di sola andata e di solo ritorno. Per Lufthansa e Iberia si conoscono solo le date: la pagina e Telegram scrivono "orario da verificare".
4. **Prezzo anomalo**: il prezzo tipico di ogni meta è la mediana dei prezzi su tutti i periodi negli ultimi 90 giorni, calcolata a parte per andata e ritorno, solo andata e solo ritorno. È anomalo se costa almeno il 50% in meno e fa risparmiare almeno 15 €.
5. **Niente doppioni** su Telegram: lo stesso volo viene segnalato di nuovo solo se scende di un altro 10%.

La pagina mostra solo i dati dell'ultima ricerca e i periodi futuri. Lo storico in `data/state.json` (pochi KB) tiene solo il prezzo mediano per meta degli ultimi 90 giorni, che serve a riconoscere i prezzi anomali; viene salvato con un commit giornaliero. Soglie, orari e fonti sono in `config.toml`.

## Pubblicazione (una sola volta)

La ricerca gira ogni giorno su **GitHub Actions** (gratis), che salva lo storico e pubblica la pagina sul branch `render`. Un **sito statico Render** (gratis) serve quel branch e si aggiorna da solo a ogni pubblicazione.

1. **Repository**: crea un repository su GitHub (anche privato) e carica questa cartella:
   ```sh
   git init -b main
   git add .
   git commit -m "Alert voli economici"
   git remote add origin git@github.com:<utente>/<repo>.git
   git push -u origin main
   ```
2. **Secret**: *Settings → Secrets and variables → Actions → New repository secret*:
   - `TELEGRAM_BOT_TOKEN`: scrivi a [@BotFather](https://t.me/BotFather), `/newbot`, copia il token.
   - `TELEGRAM_CHAT_ID`: manda un messaggio al tuo bot e apri `https://api.telegram.org/bot<TOKEN>/getUpdates` per leggere `chat.id`.
   - `TRAVELPAYOUTS_TOKEN` (facoltativo ma consigliato: aggiunge tutte le altre compagnie e **i voli intercontinentali**): registrati su [travelpayouts.com](https://www.travelpayouts.com) e copialo da *Profilo → API token*.
3. **Primo avvio**: *Actions → Voli economici → Run workflow*. A fine ricerca (40-60 minuti) compare il branch `render` con la pagina.
4. **Render**: su [dashboard.render.com](https://dashboard.render.com) *New → Blueprint*, collega il repository e conferma: `render.yaml` crea il sito statico `voli-economici`, che pubblica il branch `render`. La pagina è online su `https://voli-economici.onrender.com` (o un indirizzo simile, lo trovi nella dashboard).

Senza i secret Telegram il workflow funziona lo stesso: aggiorna solo la pagina.

## Il workflow (`.github/workflows/daily.yml`)

| Quando | Cosa fa |
|---|---|
| Ogni giorno alle 8:00 (7:00 d'inverno) o con *Run workflow* | Cerca i voli (40-60 minuti, per via di Wizz Air, Vueling e Lufthansa), invia Telegram, salva lo storico, pubblica la pagina sul branch `render` |
| Push su `main` che cambia `site/` o il workflow | Ripubblica solo la pagina, riusando i dati già online (nessuna ricerca, nessun messaggio) |

Il branch `render` contiene solo l'ultima versione della pagina (viene sovrascritto ogni volta), quindi la sua storia non cresce.

## Uso in locale

```sh
uv run python -m voli_alert --dry-run          # cerca, scrive site/data.json, stampa il messaggio senza inviarlo
python3 -m http.server 8000 --directory site   # apri http://localhost:8000
uv run pytest
```
