# CHRONOS - AI Multi-Agent Development Protocol

## Ruoli
- **Architetto (Opus):** Detiene le specifiche di alto livello, convalida la logica matematica delle decisioni (System 1 / approccio Jev) e risolve le incongruenze bloccanti.
- **Sviluppatore (Sonnet):** Scrive il codice Python effettivo, esegue i test e implementa i moduli.

## Stato Attuale dello Sviluppo
- Rewrite architetturale e logico di chronos.

## Protocollo di Risoluzione Incongruenze (Loop Autonomo)
Se lo Sviluppatore (Sonnet) incontra un blocco logico, una contraddizione o ha bisogno di chiarimenti matematici sull'approccio decisionale Jev:
1. NON chiedere all'utente umano.
2. Genera o aggiorna il file `.claude/CONFLITTO.md` descrivendo esattamente il problema, il file interessato e il dubbio logico.
3. Invoca formalmente il sub-agente `architetto-opus` passandogli il file `CONFLITTO.md`.
4. Attendi che `architetto-opus` riscriva le specifiche o risolva il dubbio nello stesso file.
5. Leggi la soluzione, cancella il file `CONFLITTO.md` e riprendi l'implementazione del codice.

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