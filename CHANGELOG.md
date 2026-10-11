# Changelog

## 2.15.2 — collector naming and GitHub fixes

- Renamed the collector module to `database_collector.py`; updated imports, commands, configuration examples and documentation. Collector version remains 7.11.0.
- Corrected current Diagnostics parser labels to 7.11.0.
- New installations default to `data/database_collector.db`. Existing installations must preserve their configured database path and set `script` to `database_collector.py`. No rebuild or backfill is required.
- Renamed the pinned dependency file to `requirements-pinned.txt` and updated Docker, CI and documentation for Dependabot compatibility.
- Maintenance backup tests follow the collector version and validate backup contents.

## 2.15.1 — restore service icons

- Restored the original SABnzbd, Prowlarr, Radarr and Sonarr SVG assets from 2.14.3.
- Retained their original icon dimensions and layout; versioned icon URLs avoid stale letter badges in browser caches.


## 2.15.0 — public beta preparation

- Shared portable configuration and database paths across web, CLI and maintenance commands.
- First-run setup page, optional offline schema initialization, Windows/Linux launchers.
- Strict placeholder, path, URL, integer and timezone validation; environment-based API keys.
- Atomic collector startup and OS-backed database lock for all collection/maintenance entry points.
- Worker cleanup on failure; output redaction; explicit partial API collection status.
- Incomplete log tails retained; time-window caches expire within 60 seconds.
- Configured attribution window used consistently in dashboard, samples and release journeys.
- Optional access-token login for LAN/Docker, host validation and same-origin write protection.
- Neutral service letter badges replace third-party logo assets with unverified redistribution provenance.
- Bundled HTMX 2.0.5; reproducible dependencies, tests, CI, MIT license and publishing guidance.
- Existing collector filename retained for configuration compatibility; collector version is 7.11.0.

## Earlier development history

These notes describe earlier upgrades and are retained for historical context. Follow the current README for installation and upgrades; old commands and folder examples below are not current setup instructions. Collector script/database names in these archived examples have been normalized to the current naming; version numbers still describe the historical releases.

## v2.14.3 shared chart hover

v2.14.3 restores the floating chart readout while keeping the calmer hover behavior. Hovering a line/area chart now selects one time bucket, highlights every visible series point in that bucket, and shows all values together in one stable tooltip. The point highlight no longer uses the pop/scale animation. The complete multi-day date-axis labeling from v2.14.2 is retained. No collector/parser or database schema change is required.

## v2.14.1 chart + metric clarity polish

v2.14.1 is a UI/clarity patch on top of v2.14.0. It does not change the collector/parser schema and does not require a database rebuild or backfill.

### Changes

- Dashboard live trends now use interactive area/line charts instead of grouped bars, with a higher-contrast series palette, hover crosshair, glowing points, exact-value tooltip, and clickable legends.
- Indexer Performance, Raw Search Results, and Provider headline metrics now use the same animated value-update glow/count transition as Dashboard metrics when an Activity Window or sample changes.
- Complex metric labels throughout the UI gain a small hover/focus `?` explanation when the metric is not already explained beside it.
- Provider article-availability trends now use the same interactive chart component as Provider traffic share over time.
- Downloads Recent Sample now uses an animated interactive donut: hovered segments lift/glow, unrelated segments fade, and a tooltip shows the exact count/percentage.
- Help was rewritten in plain language with page-by-page explanations and fewer internal implementation terms.
- Removed the `v7.10.10 cancellation scan · N lines` diagnostic row and the unused `Cancelled / aborted` metric from Indexer Performance.

### Upgrade

Keep the existing `database_collector.db`. Do **not** run `Rebuild search data.bat` or Backfill. The collector remains `database_collector.py`. If the project folder is versioned, update only the folder portion of the collector path to `sab_monitor_v2_14_1`, then restart nzbview.

---

## v2.14.0 modern motion + application branding

v2.14.0 is a visual polish release. It keeps the existing nzbview information architecture and collector/parser **v7.10.10**, while making local interactions feel more like a live monitoring application. No database Backfill or search-data rebuild is required.

### Motion and interaction polish

- the active sidebar indicator now slides between destinations instead of relying only on a static highlight
- buttons, focus states, clickable cards and table rows have restrained hover/press feedback
- HTMX navigation uses a thin route-progress cue; slower local partial swaps can show a delayed skeleton instead of flashing or animating the whole page
- collector runs pulse only while active, then changed KPI values briefly illuminate after the refreshed view arrives
- acquisition drawer/backdrop, toast notifications and Downloads season/episode expansions now use short component-scoped transitions
- Dashboard grouped bars grow from their baseline; the existing hover glow and exact-value label remain
- ranking/provider/progress bars smoothly resize when their measured value changes
- all motion honors `prefers-reduced-motion`

There is deliberately **no whole-page entrance animation**. Page navigation remains HTMX-based and only the component that changed receives motion.

### Application branding

Small local application marks are included for **Sonarr, Radarr, Prowlarr and SABnzbd**. They are used as evidence/source identity cues in Activity, Raw Search/Indexer details, Providers, Downloads, the acquisition journey and Diagnostics. Outcome colors remain responsible for success/failure/warning meaning, while the colored emoji navigation remains compact and unchanged.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. Do **not** run `Rebuild search data.bat` or Backfill. The collector remains `database_collector.py`. If the project lives in a versioned folder, update only the folder portion of the collector script path to `sab_monitor_v2_14_0` and restart nzbview.

---

## v2.13.9 season acquisition continuity + dashboard bars + new logo

v2.13.9 keeps collector/parser **v7.10.10** and focuses on presentation/correlation fixes that do not require Backfill or a search-data rebuild.

### Downloads season continuity

- Observed Sonarr whole-season searches now remain the authoritative parent while Sonarr automatically works through failed episode downloads.
- Later `automatic` and `automatic_retry` episode searches for the same series/season are folded back into the latest open season acquisition for up to 24 hours. Interactive/manual searches remain separate.
- Season children are keyed by canonical `SxxExx`, not the correlation-specific media key, so the same episode cannot split into duplicate child/top-level groups merely because a retry used different attribution evidence.
- Season completion and the collapsed completion timestamp are recomputed from the eventual child outcomes after those follow-ups are attached.

### Dashboard charts

- The four Dashboard live-trend charts now use grouped bars instead of connecting sparse buckets with lines.
- Hovering/focusing a bar highlights it, shows its exact value above the bar, and keeps the detailed tooltip.
- Existing clickable legends remain available for hiding/showing series.

### Branding

- The sidebar logo and browser favicon now use the supplied `nzbview_logo.svg`.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If you install this in a new `sab_monitor_v2_14_0` folder, update only the folder portion of the collector script path and restart nzbview.

---

## v2.13.8 season retry ownership + completion timestamps

v2.13.8 keeps the v2.13.7 evidence-backed season hierarchy and fixes a Sonarr edge case exposed by the Silo automatic season-search run. Collector/parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required.

### What changed

- Automatic episode searches launched after a failed download can now remain inside the original observed Sonarr season acquisition even when the retained Arr search is labelled `automatic` rather than `automatic_retry`. The fallback is conservative: it requires an earlier failed attempt for that exact episode, an existing directly-linked season attempt for that episode, and a follow-up within 24 hours. Interactive/manual searches remain separate.
- A season parent now reports completion using the eventual child outcomes rather than merely whether any attempt completed. A completed season pack is sufficient; otherwise every observed episode child must eventually complete.
- Collapsed Downloads rows now show a compact date/time next to the movie/show/season name. For completed media this is the final successful completion time; for incomplete media it is the latest activity time.
- Nested episode/season-pack sections show their own completion/latest-activity timestamp, while exact SAB attempt rows retain their full attempt timestamp.

## v2.13.7 evidence-backed season batches + Downloads navigation polish

v2.13.7 extends the media-first Downloads view with a second hierarchy level for real Sonarr season searches. Collector/parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required because the required `search_started`, `search_completed`, `sent_to_sab`, and SAB attempt evidence is already retained.

### Downloads season grouping

- **Observed season searches become parent groups.** A Sonarr `ReleaseSearchService` search such as `The Rookie : S06` creates a season parent only when retained Arr orchestration proves that a whole-season search actually occurred. Filename similarity alone never creates a season group.
- **Single-episode searches stay separate.** If Sonarr explicitly searches `S06E03` while an `S06` session is still open, its hand-off belongs to the episode search rather than being swallowed by the season batch.
- **Retries stay with the episode acquisition.** When an episode was handed off by a season search and later retried, its nearby retry attempts remain under the same episode child so the failure→success story is visible in one place.
- **Season packs are first-class.** A release such as `Vikings.S02...` is shown as one `📦 Season pack` child. nzbview never invents episode rows for a pack.
- **Season summaries are outcome-focused.** Collapsed season rows show completed selected episodes, total attempts, failed attempts, and pack completion where applicable. Expanding a season reveals compact successful episodes and nested retry/failure sections for episodes that need detail.
- **Chronological retry detail.** Multi-attempt episode sections show the exact SAB attempts oldest→newest so the original failure and eventual successful retry are not visually reversed.

### Interaction / pagination

- **Collapsed by default.** Download media/season groups and nested retry sections now start collapsed.
- **Expand all / Collapse all.** Two controls in the Downloads panel header operate on every visible group and nested episode section on the current page.
- **Group-safe pagination remains 12 top-level groups per page.** A season or movie/episode group is never split across pages.
- **First / Last navigation.** The shared pager now provides First and Last shortcuts in addition to Previous, Next, and direct page entry.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If you install this in a new `sab_monitor_v2_13_8` folder, update only the folder portion of the collector script path and restart nzbview.

## v2.13.6 Sonarr media-group guard + Downloads pagination

v2.13.6 is a focused correction to the media-first Downloads view introduced in v2.13.5. Collector/parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required.

### Downloads fixes

- **Wrong-series grouping guard.** An exact Arr `sent_to_sab` release-name match no longer gets to overwrite the display media identity unless the retained Sonarr/Radarr media fields also agree with that SAB release title. This prevents a stale/mis-correlated Sonarr hand-off (for example `The Wire · S05E08`) from absorbing unrelated `Prison Break`, `The Rookie`, or other episode downloads.
- **Conservative fallback remains intact.** If the retained Arr identity fails that sanity check, nzbview keeps the release-derived `Series · SxxEyy`/movie label instead of guessing. This changes presentation grouping only; download/indexer/acquisition attribution semantics are untouched.
- **Media-group pagination restored.** Downloads now shows **12 media items per page**. All attempts for one movie/episode stay together on the same page, so pagination never splits a media group. This avoids very long pages in busy activity windows.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If you install this in a new `sab_monitor_v2_13_6` folder, update only the folder portion of the collector script path and restart nzbview.

## v2.13.5 media-first Downloads + visual navigation polish

v2.13.5 makes the Downloads page attempt-centric in a way that is visually distinct from Releases. Collector/parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required.
### Downloads now starts with the movie / episode

- **Media first, release second.** SAB attempts are grouped under the retained Sonarr/Radarr media identity when an exact or conservative Arr hand-off can be matched. The exact grabbed NZB/release name stays visible underneath.
- **One media item can show several attempts.** A case such as `The Wire · S05E08` can show one failed NZB followed by another completed NZB without turning the original failure into a success.
- **Exact attempt result stays primary.** Completed / failed / cancelled / unresolved describe that specific SAB attempt. A later recovery is shown only as secondary context.
- **Expandable media groups.** Groups are open by default for easy scanning, but can be collapsed when a page contains many attempts for the same movie/episode.
- **Media-aware search.** The Downloads search box matches both the friendly movie/episode label and the exact release name.
- **Release-level final-outcome filter removed from Downloads.** That concept belongs to Releases/acquisition analysis; Downloads now filters the attempt itself.
- **Conservative fallback.** If retained Arr hand-off evidence cannot identify the media, nzbview derives only a display label from obvious `SxxEyy`/movie-year release structure. This fallback never changes attribution or grouping evidence elsewhere.

### Navigation / visual polish

- Sidebar monochrome glyphs were replaced with restrained **colored emoji** for faster visual scanning.
- Download attempt rows use colored emoji for completed, failed, cancelled, later recovered and repair context while keeping the existing text labels.
- Releases remains the unique-release / retry-chain view, so Downloads and Releases now foreground different layers of the same acquisition evidence.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If you install this in a new `sab_monitor_v2_13_5` folder, update only the folder portion of the collector script path and restart nzbview.

## v2.13.4 fixed-sample denominator + navigation-state cleanup

v2.13.4 makes the fixed-count views match the component being measured and removes the remaining state collision between Activity Window, Indexers sub-views, and Recent Sample controls. Collector parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required.

### Fixed-count semantics

- **Indexer Performance = grabs only.** Recent Sample no longer offers Searches/Downloads on the Indexers performance view. Each discovered Usenet indexer is evaluated independently on its own newest 50 / 100 / 250 / 500 / 1000 grabs. A grab counts as successful only when its linked SAB attempt completed; a failed attempt remains failed even if a later acquisition recovered the media. Unlinked/non-terminal grabs stay unknown instead of becoming failures.
- **Independent per-indexer denominators.** `Last 100 grabs` means the newest 100 grabs from altHUB, the newest 100 from NZBgeek, the newest 100 from NzbPlanet, etc. If an indexer has fewer retained grabs, nzbview shows the real available count. A separate lower panel keeps the newest N grabs overall only as selection-share context.
- **Raw Search Results = searches.** The search sub-view keeps one shared newest-N logical Arr search population. Labels now distinguish per-search hit/miss counts from individual NZB candidate counts so a result such as 1,800 matching NZBs across 500 searches is not presented as 1,800 searches.
- **Downloads = terminal SAB attempts.** The Downloads recent sample remains the newest N completed/failed SAB attempts and keeps recovery as separate context rather than rewriting an original failure.
- **Providers = downloads with provider evidence.** Provider Recent Sample now selects directly from terminal SAB History rows where SAB retained a `Servers` contribution split. If 416 such downloads exist, Last 50/100/250 selects exactly 50/100/250; Last 500/1000 reports 416 available. Generic downloads without provider evidence no longer consume the requested denominator.
- **Stable provider order.** Provider cards/bars keep a stable stored-server order rather than reordering whenever contribution percentage changes.

### Navigation/state fixes

- **Activity Window is global again.** The Activity Window selector on Recent-Sample-capable pages now participates in the same persistent window state as every other page. Selecting 14 days on Indexers/Providers/Downloads therefore stays 14 days when navigating elsewhere.
- **Indexer sub-view owns its controls.** `Indexer performance` and `Raw search results` now swap one complete Indexers suite containing the sub-tabs, scope controls and analytics body. A Raw Search Results sample-size/window change can no longer submit through a stale `/indexers` form and jump back to Indexer Performance.
- **Less reload-like flashing.** The page-wide entrance animation was removed. HTMX still swaps the main workspace for genuine sidebar navigation, while local tabs/filters continue to replace only their own workspaces.
- **Naming.** `Download performance` is now **Indexer performance**.

### Provider evidence model

Time Window and Recent Sample deliberately use different retained SAB evidence:

- **Time Window** uses periodic SAB `server_stats` snapshots for article availability and bandwidth/counter deltas.
- **Recent Sample** uses per-download SAB History `Servers` stages for contribution share and provider usage.

The two sources are not blended to fill gaps because cumulative server counters cannot prove which provider supplied an individual download.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If the release lives in a new `sab_monitor_v2_13_4` folder, update only the folder portion of the collector script path and restart nzbview.

## v2.13.3 Recent-sample semantics + partial-navigation pass

v2.13.3 tightens the meaning of each fixed-count population and removes more whole-page HTMX redraws. Collector parser logic remains **v7.10.10**; no Backfill or search-data rebuild is required.

### Recent sample now answers one question per population

- **Searches = availability.** The newest N logical Arr searches are selected globally. Per-indexer labels now use plain search language: queried searches, searches with a known wanted target, searches with/without a match, wanted-quality NZBs, and total NZBs returned. Candidate counts are explicitly documented as release counts, so they may be much larger than the number of searches.
- **Grabs = selection share.** The newest N Prowlarr Usenet grabs now answer “which indexer supplied the releases Arr selected?” The primary table shows grab count/share and how many could be safely linked to SAB. Download success/repair is deliberately removed from the grab comparison instead of duplicating Downloads.
- **Downloads = original SAB result.** The sample is the newest N genuine completed/failed SAB attempts. Cancellations/unresolved hand-offs no longer displace a real download from the fixed-count denominator. A failed NZB remains failed even if another later attempt recovered the release; recovery is shown only as secondary context.
- **Downloads page stays attempt-focused.** Its summary now shows successful vs failed downloads plus repair burden; Indexers owns the per-indexer comparison.

### Navigation/UI changes

- Scope / Sample of / Sample size / Activity window changes on Indexers, Raw Search Results, Downloads and Providers now swap only that page's analytics workspace; the page header/control bar stays mounted.
- Indexers ↔ Raw Search Results is now a true in-place Indexers sub-view and no longer replaces the entire page content.
- Activity KPI links, alert links, sub-tabs, filters and paging all target the Activity workspace.
- Releases, Recovered and Failures search/filter/sort/paging now refresh only their local table workspace. Help topic changes refresh only the Help workspace.
- The mounted scope form follows the current Indexers sub-view route after partial swaps, preventing stale-route scope changes.

### Upgrade

Keep the existing `database_collector.db` and working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill.** The collector remains `database_collector.py`. If you install this in a new `sab_monitor_v2_13_3` folder, update only the folder portion of the collector script path and restart nzbview.

## v2.13.2 Recent sample analytics

v2.13.2 includes a second analytics scope alongside the existing Activity Window. **Time window** continues to answer time-based questions; **Recent sample** answers fixed-count questions such as “how did the last 100 downloads perform?” regardless of how long those observations took to accumulate. Collector parser logic remains **v7.10.10** and no database/search rebuild is required.

### v2.13.2 navigation/state fixes

- **Scope controls are now ordinary GET form state.** Scope, Sample of, Sample size, and Activity window are submitted together by HTMX instead of being reconstructed from `window.location`. This removes the race where a rendered Recent sample page could send a stale Time-window URL.
- **Recent-sample filters keep their denominator.** Downloads and Raw Search Results carry `scope=sample`, sample type, and sample size in their own filter requests. Search/sort/filter/paging therefore operates inside the fixed sample rather than returning to Time window.
- **Localized HTMX updates.** Downloads filters refresh only the download table/filter workspace, Raw Search Results filters refresh only the observed-search workspace, and Activity sub-tabs/filters refresh only the Activity workspace. The page header, KPIs and unrelated panels no longer visibly redraw for those interactions.
- **No evidence or metric changes.** This is a UI/navigation correction on top of v2.13.1; recent-sample calculations, v2.12.11 performance work, and collector v7.10.10 remain unchanged.

- **Mutually exclusive scope modes.** Indexers, Raw Search Results, Downloads and Providers now expose `Scope: Time window | Recent sample`. Only one denominator is active at a time, so a 3-day window never silently truncates a requested 500-download sample.
- **Fixed sample sizes.** Recent sample supports the newest **50 / 100 / 250 / 500 / 1000** observations. If fewer exist, nzbview shows the real available count instead of pretending the requested denominator exists. The displayed oldest/newest timestamps are context only.
- **Indexer sample populations.** Indexers can compare the newest logical **Searches**, Prowlarr Usenet **Grabs**, or SAB **Downloads**. The global sample is selected first, then each indexer is evaluated inside that same population; indexers never receive separate historical ranges.
- **Recent-search availability.** Raw Search Results can be scoped to the newest N logical Arr searches. Wanted-quality availability keeps the existing evidence rules: only target-known queried opportunities are scored, wrong-media candidates are excluded, and unresolved targets remain unscored.
- **Recent-download performance.** Downloads freezes the table to the newest N SAB attempts before normal search/sort/filter controls are applied. Sample panels show completed attempts, failed attempts later recovered, failed-only attempts, cancellations/unresolved rows, repair load and per-indexer success among completed/failed attempts.
- **Recent-grab performance.** Prowlarr grab events are matched conservatively to SAB attempts when evidence permits. Unmatched grabs stay unresolved instead of receiving a guessed outcome.
- **Provider contribution sample.** Providers can answer which server supplied the most data across the newest N downloads using retained SAB History `Servers` stages. True article availability remains Time-window-only because SAB exposes that as cumulative server counters, not per-job counters.
- **Generation-aware sample cache.** Recent-sample results are cached by population + sample size + SQLite generation, so switching between 50/100/250/etc. remains fast until the collector actually changes the DB.

### Upgrade

Keep the existing `database_collector.db` and your working `collector_config.json`. **Do not run `Rebuild search data.bat` or Backfill for this upgrade.** The collector remains `database_collector.py`. If this release lives in a new `sab_monitor_v2_13_2` folder, update only the folder portion of the collector script path, then restart nzbview.

## v2.12.11 performance pass

v2.12.11 focuses on the slow 7/14/30/60-day navigation path. It does **not** change metric definitions, search/retry classification, acquisition grouping semantics, or retained evidence. Collector parser logic remains **v7.10.10** and no search-data rebuild is required.

- **Generation-aware analytical cache.** Snapshot and Activity results now remain cached for each activity window until the SQLite database/WAL actually changes. The old 20-second expiry could force a full rebuild simply because you spent some time reading a page before switching tabs. Different windows are also retained independently, so switching 7d -> 14d -> 7d reuses the already-built 7d result when the DB is unchanged.
- **Window-bounded Raw Search Results reconstruction.** Search sessions, candidates, targets and Prowlarr query evidence are read only for the selected window. Prowlarr query correlation now binary-searches the retained timeline for the actual -2s/+45s search interval instead of rescanning every retained query for every Arr search.
- **Faster Activity correlation.** Prowlarr log/API duplicate suppression uses per-indexer timestamp indexes, and each search/RSS session slices only its relevant Prowlarr time interval. This removes the main quadratic hot path that became visible as retained query volume grew.
- **Bounded Activity attribution history.** Activity loads only the Prowlarr grab evidence needed for its selected window plus the existing 168h attribution look-back. Snapshot release grouping remains full-history so unusual long retry chains keep exactly the same semantics as v2.12.10.
- **Same output semantics.** The optimized paths were regression-checked against v2.12.10 on a synthetic dataset shaped like the current database (hundreds of searches/attempts and tens of thousands of Prowlarr query rows); Activity and key snapshot/search-coverage outputs remained identical while the hot paths became substantially faster. Exact timings will vary by PC and retained data.

### Upgrade

Keep the existing `database_collector.db` and your working `collector_config.json`. **Do not run `Rebuild search data.bat`.** The collector remains `database_collector.py`; if you place this release in a new `sab_monitor_v2_12_11` folder, update only the folder portion of `collector_config.json` so it points at that same collector script in the new folder. Restart nzbview. A collector run is not required for the performance changes themselves.

## v2.12.10 interactive chart polish

v2.12.10 is a UI/observability polish pass on v2.12.9. It does not change search/retry scoring and does not require a search-data rebuild.

- **Point hover tooltips.** Dashboard multi-series trends and the Providers traffic-share trend now snap to the nearest retained bucket and show every visible series value for that point. Percentage charts also expose their retained sample size where that denominator is meaningful (releases, target opportunities, or contributing jobs).
- **Clickable chart legends.** Every multi-series Dashboard/Providers legend can be clicked to hide/show a series. The axis remains time-stable while comparing services, and the value scale recalculates from the visible series. Indexer/provider identities remain data-driven rather than hardcoded.
- **Finer adaptive buckets.** Common activity windows now target roughly 12–15 chart buckets (2h for 1 day, 6h for 3 days, 12h for 7 days, 24h for 14 days, 48h for 30 days, and 96h for 60 days) so short windows are less sparse without creating noisy long-window plots.
- **Provider labels.** SAB server configuration parsing now tolerates nested/list/dict shapes and prefers configured display/name values for chart legends. Older hostname-only snapshots fall back to a generic readable label without a fixed provider registry.
- **Indexer sidebar state.** Raw Search Results remains a sub-view of Indexers, so the Indexers sidebar item stays highlighted during HTMX navigation.

### Upgrade

Keep the existing SQLite database. No rebuild is required. Carry over your working `collector_config.json`, update only its `script` path to `database_collector.py`, start nzbview, and run the collector once. The collector run is only needed to refresh optional provider display/name metadata; the chart and navigation changes work immediately.

# nzbview v2.13.3 + collector v7.10.10

A local FastAPI/Jinja/HTMX monitor for SABnzbd + Prowlarr, with Sonarr/Radarr orchestration context.

## v2.12.8 Usenet-only raw-search pass

Raw Search Results excludes torrent/non-NZB Prowlarr indexers using Prowlarr's stored protocol metadata rather than indexer-name heuristics. Historical raw-search rows and aggregate availability therefore remain focused on Usenet indexers only.

When a historical search target cannot be positively reconstructed, the UI also surfaces retained negative Arr profile evidence (for example, observed 1080p/720p candidates explicitly marked "not wanted in profile") while keeping the target unresolved and unscored. This does **not** guess that the wanted target was 2160p.

This pass did not require a search-data rebuild; the filtering and negative-evidence hint are computed from data already in SQLite.

## v2.12.7 search-origin and acquisition-scope pass

v2.12.7 fixed the two remaining correlation issues exposed after the v2.12.6 rebuild:

- **Automatic vs Interactive is structural, not delay-based.** Radarr's `MovieSearchService|Performing search...` marker is treated as a one-shot parent intent for the immediately following `ReleaseSearchService` query. It is consumed after that search instead of remaining active for minutes. Historical reclassification uses the same parent/completion evidence, clears stale ordinary `automatic` labels that cannot be re-proven, and then lets mature standalone result sessions with a later hand-off classify as `interactive`. This is the Transcendence case: the 14:37:15 search remains Automatic, while the standalone 14:37:55 search becomes Interactive/high.
- **Release statistics and drawers are acquisition-scoped.** Repeated downloads of the same exact NZB/title are split when consecutive observations are more than six hours apart. The Releases page therefore no longer reports a two-days-apart Vikings repeat as a `2 attempts` retry chain, and the release drawer applies the same six-hour bound to exact Arr events so an old unresolved hand-off cannot leak into a later acquisition. Genuine short retry/recovery chains such as Tulsa King remain grouped.

That pass required one `Rebuild search data.bat` run so retained Sonarr/Radarr logs could reclassify historical search origins such as Transcendence.

## v2.12.6 correctness and retention pass

v2.12.6 addressed four evidence-correlation issues exposed by the September 10 test set:

- **Time-correct search targets.** A live Sonarr/Radarr API profile snapshot is no longer back-applied when the same search's retained Arr decision stream explicitly disproves that target. Existing `arr_api_snapshot` rows are revalidated during the search-layer rebuild. This prevents a later 1080p profile from rewriting an earlier temporary 4K search such as The Bank Job or Transcendence. If the old logs prove only that 1080p was unwanted but do not retain enough positive 2160p evidence, that search remains target-unresolved rather than being assigned a guessed profile.
- **Explicit search outranks overlapping RSS.** Release drawers bind Prowlarr query context to the explicit Arr search start when one is retained. A long-running Radarr/Sonarr search therefore cannot be relabeled as an RSS check merely because a zero-grab RSS sync overlaps while Arr is still evaluating candidates.
- **Repeated releases are separate acquisitions in drawers.** The same exact NZB downloaded days apart is split into separate drawer clusters, while genuine short retry/recovery chains such as Tulsa King remain grouped.
- **SAB rotation continuity diagnostics.** Each SAB family run records whether at least one current content-identified segment overlaps the previously observed family. Diagnostics reports `baseline`, `ok`, `unverified`, or `possible gap`. A no-overlap warning is evidence of uncertain continuity, not a claim that data was definitely lost.

## v2.12.5 Activity selection-evidence patch

v2.12.5 made Activity treat an `arr_search_candidates.decision = selected` row as direct evidence that its logical Arr search selected a release. A later `Sent to SAB` event remains supporting/fallback evidence, but is no longer required for the search outcome to remain `release selected`. This fixed Tulsa King S01E01 at 22:29:01.

## v2.12.4 trigger attribution correctness patch

v2.12.4 tightened historical retry reclassification so an unanchored retry intent cannot mark an unrelated search as `automatic_retry`, and made Activity prefer a compatible explicit Arr search/retry session over an overlapping RSS cycle. This fixed the Tulsa King / House of the Dragon cross-media retry and RSS-ownership cases.

## v2.12.3 acquisition-journey patch

v2.12.3 introduced media-scoped retry matching and acquisition-oriented episode drawers so differently named NZBs for the same short retry/recovery flow can be shown together. The fresh Tulsa King S01E01 sequence is represented as initial Automatic → altHUB failure → Automatic Retry → altHUB failure → Automatic Retry → NZBgeek completion.

## v2.12.2 derived search-layer rebuild

This patch adds the migration that v2.12.1 proved we needed: a **rebuild of the derived Prowlarr/Arr search layer** instead of another additive replay. It creates a WAL-safe SQLite backup first, preserves SAB lifecycle/history/provider data and existing Arr orchestration events, preserves exact API quality-profile snapshots, clears only derived raw-candidate/Prowlarr-query/inferred-target rows, then replays the retained Prowlarr/Sonarr/Radarr rotations from byte 0 with collector v7.10.2.

The rebuild also reconstructs historical wanted-quality evidence from Arr's own per-release decision stream. A correct-media quality can count as historically profile-compatible even when Arr later rejected the release for Custom Format score, size, language, retention, delay, or another downstream reason. Qualities explicitly logged as `not wanted in profile` are excluded. If lower fallback resolutions were allowed alongside a higher compatible resolution, only the highest observed compatible resolution tier is scored.

Preferred command from the nzbview folder:

```bat
python rebuild_search_data.py
```

Or double-click **Rebuild search data.bat**. Searches with no retained quality-profile evidence at all—especially zero-result searches that occurred before API snapshots existed—can still remain unresolved; the rebuild does not invent a profile that was never retained.

## Raw Search Results / indexer availability

This release separates **raw indexer availability** from the existing download/outcome metrics. Raw availability asks one question only: when Sonarr/Radarr searched for media with a known quality-profile target, did each queried indexer return at least one correct-media result in that wanted quality?

The collector retains Arr-parsed candidate quality before Custom Format, size, language, release-group, retention, download, repair, or import decisions. Those later decisions do not affect raw availability. Explicit wrong-media parses are excluded, so an indexer metadata false-positive does not receive credit. Duplicate releases returned through multiple query strategies are counted once per logical Arr search/indexer.

New searches use a time-bounded Sonarr/Radarr API quality-profile snapshot. v2.12.2 also adds a **derived search-layer rebuild** for retained historical logs. Historical targets are reconstructed from Arr's own per-release decision stream: correct-media qualities that passed the quality-profile check are retained even when the release was later rejected for Custom Format score, size, language, retention, or another downstream rule; qualities explicitly marked `not wanted in profile` are excluded. When lower fallback resolutions were allowed, only the highest profile-compatible resolution tier observed in that logical search is scored. Searches without enough retained evidence remain **target unresolved** rather than being assigned today's profile.

The Indexers area now has two independent views:

- **Download performance** — attempts, successful grabs, final unique-release success, repair/recovery and Prowlarr history activity.
- **Raw search results** — target opportunities, target hits/misses, matching raw-result counts and per-search/indexer drill-down.

A miss means the configured indexer did not return the requested quality for that search. It does **not** prove the underlying Usenet articles are absent from Newshosting, NewsDemon, or another provider.

The collector also propagates late SAB PAR-block observations back into the corresponding stored job so release drawers and repair statistics do not stay at zero when repair blocks were discovered after the initial job row was created.

## Rebuild historical raw-search data

After upgrading an existing long-lived database, stop nzbview/the collector and run:

```bat
python rebuild_search_data.py
```

or double-click **Rebuild search data.bat**. The command creates a WAL-safe backup first, preserves SAB/history/provider data and exact API target snapshots, clears only derived raw-search/query/inferred-target rows, replays the retained Prowlarr/Sonarr/Radarr rotations from byte 0 with collector v7.10.10, revalidates mutable API targets against same-search Arr decisions, reconstructs historical profile-compatible quality targets, and leaves the incremental cursors at EOF.

The older `backfill_database.py` additive replay remains available for diagnostics, but the derived search-layer rebuild is the recommended migration for databases that were populated by pre-v2.12 collectors.

## What is new

### Normal + debug rotating-log families

v7.8.6 automatically reads both normal and debug rotations from the existing Prowlarr/Sonarr/Radarr log directories, including families such as `sonarr.txt`, `sonarr.0.txt`, `sonarr.debug.txt`, `sonarr.debug.1.txt`, and their Radarr/Prowlarr equivalents. No additional config paths are required. Duplicate INFO lines that appear in both normal and debug files are deduplicated, while only high-value debug evidence is retained.

The first collector run after upgrading may take longer because retained debug rotations are backfilled. Later runs continue incrementally from stored segment offsets.

### Evidence-based search triggers

Search origin now distinguishes:

- **Automatic** — Arr automatic search intent/completion is retained.
- **Automatic retry** — `RedownloadFailedDownloadService` explicitly says a failed download is being searched again. This is causal evidence rather than timing correlation.
- **Interactive** — a matured result-processing session followed by a matching release hand-off without an automatic completion is high-confidence interactive/manual selection; browse-only sessions remain medium-confidence interactive.
- **RSS** — retained RSS context.
- **Origin unclear** — evidence is insufficient.

Search-only browsing is still never counted as a bad release.

### Explicit Sonarr import + Arr history cleanup

Sonarr debug `DownloadEventHub` signals such as `Marking download as imported from SABnzbd` are now retained as explicit import confirmation. Radarr's existing explicit movie-file assignment signal remains supported. Generic verbose import/debug chatter is deliberately ignored so candidate-file decisions do not create false import successes or failures.

Arr history removals are classified separately:

- `arr_remove_completed_history` — routine cleanup after confirmed import.
- `arr_remove_failed_history` — failed item removed before/while Arr redownloads.
- `unknown_removal` — retained for review when context is insufficient.

These actions are not SAB cancellations and do not affect indexer-quality failure denominators.

### Cancellation safety remains strict

The retained SAB cancellation reconciliation from v7.8.5 remains in place. `Removing job` and `Purging data ... (delete_all_data=True)` are not sufficient by themselves because successful jobs emit those lines too. Normal `Ending job -> Removing job -> Starting Post-Processing` transitions and successful post-processing purges are rejected. Queue cancellation requires cancellation context with no normal post-processing path, and `Aborting DirectUnpack` is strong supporting evidence when present.

### Release journeys and diagnostics

Release drawers now show **Automatic retry** and only call a follow-up causal when Arr retained the explicit failed-download retry trigger. Otherwise the wording stays correlation-only. Diagnostics shows explicit auto-retry and Arr history-removal counts, and its rotating-log coverage explains the normal + debug families.

## Upgrade

Reuse your existing SQLite database:

```text
C:\nzbview\database_collector.db
```

Update `collector_config.json` so the script points at the new collector while the database continues to point at the existing DB:

```json
{
  "script": "C:\\nzbview\\sab_monitor_v2_12_11\\database_collector.py",
  "db": "C:\\nzbview\\database_collector.db"
}
```

Keep your existing SAB/Prowlarr API keys and log-directory settings. Also configure `sonarr_url`/`sonarr_api_key` and `radarr_url`/`radarr_api_key` if you want new searches scored against the exact quality-profile target active at search time. The existing Prowlarr, Sonarr and Radarr log directories remain enough for raw candidate ingestion.

Run the collector once after upgrading, then check **Diagnostics -> Last collector run**. It should show collector `7.10.10`. The first run may scan a lot of retained debug lines; subsequent runs are incremental.

Start the web app either with the included **`Start nzbview.bat`** (recommended for your folder layout) or manually:

```powershell
python .\run.py --db "C:\nzbview\database_collector.db" --quiet
```

The BAT keeps Python in the visible console process, opens the browser after a short delay, and uses the database one folder above the app directory. Keep its console window open while nzbview is running; closing that console stops the web app.

Then open `http://localhost:8787` if it did not open automatically.

## Recommended log directories

- SABnzbd: `C:\Users\YOUR_USER\AppData\Local\sabnzbd\logs`
- Prowlarr: `D:\Services\prowlarr\logs`
- Sonarr: `D:\Services\sonarr\logs`
- Radarr: `D:\Services\radarr\logs`

## Metric reminder

Indexer quality, provider article availability/contribution, SAB terminal outcome, Arr import and Arr history cleanup are separate populations. Search-only browsing, cancellations, unresolved hand-offs, pre-SAB failures and post-download import problems are never silently converted into indexer-quality failures.
