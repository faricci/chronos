# Chronos v2 - stato in parole semplici

Ultimo aggiornamento: 2026-10-27. Dettagli tecnici: `docs/specs/chronos-system1-spec.md` (la spec) e `tasks/todo.md` (il registro dei lavori).

## In breve

- L'infrastruttura e' completa: dati, indicatori, modello, rischio, simulazione degli ordini, report notturni. Il dry run (prova su dati reali senza soldi) gira dal 19 ott.
- Nessun vantaggio dimostrato. Il lavoro sul modello e' fermo al 18 ott: sui soli prezzi nessuna strategia guadagna dopo i costi, e le news non danno un segnale d'ingresso. Il modello attuale non compra mai.
- Una prova a taglia minima (pochi euro, per verificare ordini reali) e' in preparazione: esiste solo il piano, nessun codice e nessuna decisione presa.
- Nessun ordine reale e' possibile: l'accesso a Coinbase e' in sola lettura e il codice ha solo il gateway simulato.

## Regole che non si toccano

- Solo mercato spot: niente leva, niente margin, niente futures.
- Ogni ordine e' coperto al 100% dal saldo reale; con saldo in cache vecchio o insufficiente l'ordine non parte.
- Il rischio massimo e' il capitale assegnato all'ordine, mai un debito.

## Dove siamo

| Fase | Stato |
|------|-------|
| P0 Dati e indicatori | Finito. Il controllo sulle candele riviste e' passato sui giorni 24-25 ott per BTC, ETH, SOL (i prodotti del modello). XRP, che non fa parte del modello, e' solo diagnostica |
| P1 Vantaggio statistico | Codice finito, risultato negativo (vedi sopra) |
| P2 Collegamento a Coinbase | Finito e testato, solo simulazione |
| P3 Dry run | In corso con supervisore. Il controllo di fine prova (G-DRY-OPS) conta i giorni dal 24 ott e resta incompleto: la deriva delle feature non e' certificabile e viene solo sorvegliata |
| P4-P6 Soldi veri, piu' grandi, cloud | Non iniziato: manca un modello con un vantaggio dimostrato |
| P7 Interfaccia web | Solo progettata |

## Da decidere

- Cosa fare sul modello: altri approcci, altri dati, o fermarsi.
- Se e come fare la prova a taglia minima: il piano e' in `docs/decisions/2026-10-26-p4-prova-esecuzione-minima-piano.md`.

## Dove guardare

- Decisioni spiegate: `docs/decisions/`. Errori passati e regole: `tasks/lessons.md`.
- Report del dry run: `data/reports/dry/` e `data/reports/learner/` nella cartella `chronos-dryrun`.
- Il `README.md` nella radice descrive ancora la v1 e va riscritto.
