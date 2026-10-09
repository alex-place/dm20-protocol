# Istruzioni Progetto: dm20-protocol

## Lingua

Questo è un progetto internazionale. **Tutti i contenuti del repository devono essere in inglese.**

### Contenuti in INGLESE (obbligatorio)

Qualsiasi file o testo che finisce nel repository o su GitHub deve essere scritto in inglese:

**Git & GitHub:**
- Commit messages
- Branch names
- Pull Request (titolo e descrizione)
- Issue titles e body
- Labels e milestones
- Commenti su PR e Issues

**File nel repository:**
- README.md, CHANGELOG.md
- Commenti nel codice
- Docstrings e documentazione API
- File di configurazione con descrizioni

**File generati da Claude Code:**
- PRD (`.claude/prds/*.md`)
- Epic e Task files (`.claude/epics/**/*.md`)
- Qualsiasi altro file markdown generato
- Script e utility

### Comunicazione in ITALIANO

La comunicazione diretta con l'utente avviene in italiano:
- Risposte e spiegazioni durante la sessione
- Domande di chiarimento
- Riepiloghi e status updates

**Regola semplice:** Se va nel repo o su GitHub → inglese. Se è una risposta all'utente → italiano.

## Git Remote Configuration

Questo repository è un progetto indipendente su `Polloinfilzato/dm20-protocol`.

| Remote | URL | Permessi |
|--------|-----|----------|
| `origin` | `Polloinfilzato/dm20-protocol` | Lettura/Scrittura |

### Regole di Interazione con GitHub

Tutte le operazioni GitHub usano il remote `origin` (`Polloinfilzato/dm20-protocol`):
- **Push di branch e tag** → `git push origin <branch>`, `git push origin --tags`
- **Creazione di Issues** → `gh issue create --repo Polloinfilzato/dm20-protocol`
- **Creazione di Labels** → `gh label create --repo Polloinfilzato/dm20-protocol`
- **Modifica di Issues** → `gh issue edit --repo Polloinfilzato/dm20-protocol`
- **Creazione di Pull Request** → `gh pr create --repo Polloinfilzato/dm20-protocol`
- **Qualsiasi comando `gh`** → aggiungere sempre `--repo Polloinfilzato/dm20-protocol`

### Gestione Issue Lifecycle

Quando un task/issue viene completato:
1. Aggiornare lo status nel file locale (`.claude/epics/{epic}/{task}.md`) → `status: completed`
2. **Chiudere la issue su GitHub** con commento di completamento:
   ```bash
   gh issue close {numero} --repo Polloinfilzato/dm20-protocol --comment "✅ Completed. {breve descrizione}"
   ```
3. Aggiornare `execution-status.md` se presente

## Memoria di Progetto

**IMPORTANTE:** Prima di eseguire comandi shell o iniziare un task, consultare SEMPRE la memoria di progetto in:

```
/Users/ema/.claude/projects/-Users-ema-Library-Mobile-Documents-com-apple-CloudDocs-GitHub-la-Clonazione-gamemaster-mcp/memory/MEMORY.md
```

Contiene informazioni critiche su:
- **Compatibilità macOS** — comandi che non funzionano come su Linux (es. `grep -P`, `sed -i`, `date -d`)
- **Configurazione GitHub** — remote origin
- **Convenzioni di progetto** — naming, pre-analisi task, lezioni apprese

Non ripetere errori già documentati nella memoria.

## PM Scripts Path

When `/pm:*` commands reference shell scripts, they are located at:

```
.claude/scripts/pm/
```

**NOT** at `ccpm/scripts/pm/` (which is the default path in other projects).

## Claude Code plugin

`plugin/dm/` is generated from `.claude/` (commands, `dm-persona.md`, agents). After editing any of them, run `python plugin/build_plugin.py`; CI and `tests/test_plugin_package.py` fail when the plugin is out of date.
