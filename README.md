# Preiswächter für Kleinanzeigen

Beobachtet Kleinanzeigen zu frei wählbaren Produkten, speichert jede Stunde die
Preisentwicklung (Median, günstigster Preis) und schickt dir eine Push-Nachricht aufs
iPhone, sobald eine Anzeige deutlich günstiger ist als üblich.

```
index.html                    Web-App (GitHub Pages) – Diagramme, gute Angebote
config/watchlist.json         Deine Produkte (auch in der App änderbar)
data/history.json             Preisverlauf (wird automatisch geschrieben)
scraper/watcher.py            Abruf, Analyse, Benachrichtigung
.github/workflows/watch.yml   Zeitplan: läuft stündlich bei GitHub
```

## Einrichtung (einmalig)

1. **Repository anlegen** auf github.com (öffentlich, sonst gibt es im Gratis-Tarif kein GitHub Pages)
   und den gesamten Inhalt dieses Ordners hochladen (inkl. des versteckten Ordners `.github`).
2. **GitHub Pages aktivieren:** Settings → Pages → Source „Deploy from a branch“ → Branch `main`, Ordner `/ (root)`.
   Die App ist danach unter `https://DEINNAME.github.io/REPONAME/` erreichbar.
3. **Schreibrechte für den Workflow:** Settings → Actions → General → Workflow permissions →
   „Read and write permissions“.
4. **Push-Benachrichtigung einrichten:**
   - iPhone: App **ntfy** installieren und ein Thema abonnieren mit einem langen, zufälligen Namen
     (z. B. `preiswaechter-x7k2q9m4`). Wer den Namen kennt, kann mitlesen – also geheim halten.
   - GitHub: Settings → Secrets and variables → Actions → New repository secret →
     Name `NTFY_TOPIC`, Wert = dein Themenname.
5. **Ersten Lauf starten:** Tab Actions → „Preise prüfen“ → Run workflow. Nach ca. 1 Minute
   stehen die ersten Daten in der App. Später geht das auch per Button „↻ Jetzt prüfen“ in der App (braucht das Token aus den Einstellungen).

## Produkte auswählen

- **In der App:** ⚙︎ → Repository (`name/repo`) und Token eintragen, dann „+ Produkt“.
  Token erzeugen: GitHub → Settings → Developer settings → Fine-grained tokens →
  nur dieses Repository, Berechtigungen „Contents: Read and write“ und „Actions: Read and write“. Er wird nur im Browser des jeweiligen Geräts gespeichert.
- **Oder direkt** in `config/watchlist.json` (GitHub-Weboberfläche, Stift-Symbol).

| Feld | Bedeutung |
|---|---|
| `query` | Suchbegriff wie bei Kleinanzeigen |
| `min_price` / `max_price` | Preisspanne – `min_price` blendet Zubehör und Lockangebote aus |
| `target_price` | Fester Wunschpreis. Leer lassen für die automatische Variante |
| `deal_percent` | Automatisch: „gut“ = so viel % unter dem Normalpreis (Median der letzten 30 Tage) |
| `exclude` | Wörter, die im Titel nicht vorkommen sollen (defekt, bastler, …) |
| `max_pages` | Wie viele Ergebnisseiten pro Lauf (1–5) |

## Hinweise

- Die automatische Schwelle braucht mindestens 6 Anzeigen; der Normalpreis wird mit jedem Lauf genauer.
- Du bekommst jede Anzeige nur **einmal** gemeldet.
- Kleinanzeigen kann automatisierte Abrufe blockieren oder sein Seitenlayout ändern. Dann
  schlägt der Lauf unter Actions rot fehl – die Anpassung steckt in `ListingParser` in `scraper/watcher.py`.
- Bitte Abrufhäufigkeit und Seitenanzahl moderat halten; die Nutzungsbedingungen von Kleinanzeigen
  schränken automatisiertes Auslesen ein.
