# CHRONOS - AI Multi-Agent Development Protocol

## Ruoli del Team AI
- **Trader / Analista Quant (Finanza):** Detiene le regole matematiche dei mercati, i vincoli di risk management (NO LEVA, COPERTURA 100%) e le strategie di trading.
- **Architetto (Opus - Struttura):** Convalida l'architettura software, definisce i payload JSON e traduce le strategie finanziarie in specifiche tecniche.
- **Sviluppatore (Sonnet - Codice):** Esegue materialmente i file, scrive il codice Python e implementa i test.

## Protocollo di Risoluzione Avanzato
Se lo Sviluppatore (Sonnet) incontra un blocco logico, una formula matematica ambigua o una discrepanza nei dati finanziari:
1. NON chiedere all'utente umano.
2. Genera o aggiorna il file `.claude/CONFLITTO.md` descrivendo esattamente il problema, il file interessato e il dubbio logico.
3. Invoca prima `trader-quant` se il dubbio è finanziario/strategico (es. "Come calcolo l'ATR dinamico per lo Stop-Loss?").
4. `trader-quant` risponde nel file scrivendo la regola matematica finanziaria.
5. Invocare poi `architetto-opus` passandogli sia il dubbio iniziale che la risposta matematica del `trader-quant`. L'architetto scriverà le specifiche software finali in `.claude/CONFLITTO.md`.
6. Quando riprendi il controllo, leggi la soluzione dell'architetto, implementa il codice nel file di destinazione, svuota/elimina il file `CONFLITTO.md` per segnalare la fine della sessione di blocco e procedi con il task successivo.

## ⚠️ VINCOLI FINANZIARI ASSOLUTI (SCOPLITI NELLA PIETRA)
1. **DIVIETO ASSOLUTO DI LEVA (NO LEVERAGE / NO MARGIN):** Il bot deve operare ESCLUSIVAMENTE sul mercato SPOT. È vietato l'utilizzo di contratti Futures, Margin Trading, Opzioni o qualsiasi strumento a leva. Ogni operazione deve essere coperta al 100% dal saldo reale.
2. **COPERTURA TOTALE DEGLI ORDINI:** Prima di inviare qualsiasi ordine, il bot deve verificare che il saldo disponibile sia >= all'importo + commissioni. 
   - *Specifica Tecnica:* Il saldo non deve essere richiesto via API sincrona al momento dell'ordine (causerebbe latenza). Deve essere mantenuto in una cache locale in memoria (Memory Cache / State Manager) costantemente aggiornata in background tramite un thread asincrono o WebSocket dedicato. Se la cache è obsoleta o insufficiente, l'ordine viene ABORTITO immediatamente.
3. **PROTEZIONE DAL DEBITO:** Nessuna logica passata, presente o futura può esporre l'account a saldi negativi. Anche per micro-ordini (es. 5 euro), il rischio massimo deve essere limitato esclusivamente al capitale spot allocato a quell'ordine.

## Workflow Orchestration

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately - don’t keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy

- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop

- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done

Never mark a task complete without proving it works

Diff behavior between main and your changes when relevant

Ask yourself: “Would a staff engineer approve this?”

Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)

- For non-trivial changes: pause and ask “is there a more elegant way?”
- If a fix feels hacky: “Knowing everything I know now, implement the elegant solution”
- Skip this for simple, obvious fixes - don’t over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don’t ask for hand-holding
- Point at logs, errors, failing tests - then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### Task Management

1. Plan fist: Write plan to ‘task/todo.md’ with checkable items
2. Verify Plan: Check in before starting implementation
3. Track Progress: Mark items complete as you go
4. Explain changes: High-level summary at each step
5. Document results: add review section to ‘tasks/todo.md’
6. Capture Lessons: Update ‘task/lessons.md’ after corrections

### Core Principles

- Simplicity First: Make every change as simple as possible. Impact minimal code.
- *No Laziness*: Find root causes. No temporary fixes. Staff developer standards
- Mininal Impact: Changes should only touch what’s necessary. Avoid introducing bugs.