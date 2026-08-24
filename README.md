# ha-registry-clean

Tools for clearing out what Home Assistant leaves behind when an integration is
deleted, so that reinstalling it starts from a clean slate instead of inheriting
the entity_ids and names of the install before it.

```
scripts/
└── pyscript/
    └── integration_registry_cleanup.py
```

Scripts are grouped by the runtime they need; `pyscript/` holds those that run
inside the [pyscript](https://github.com/custom-components/pyscript) custom
integration.

## ⚠️ Read this first

**Take a full Home Assistant backup before you use this.** A live run edits your
entity and device registries — the records every entity_id, name, area and
customisation in your installation hangs off. A mistake here is not cosmetic.

The tool tries hard to make that safe: it defaults to a dry run, it shows you
exactly what it will remove before it removes anything, it never touches a
registry entry that a config entry still owns, and with `backup: true` it copies
the registry files aside first. None of that is a substitute for a backup you
made yourself. In particular, `purge_statistics` deletes rows from the recorder
database, which the tool's own backup does **not** cover.

**This software is provided "as is", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability, fitness
for a particular purpose and noninfringement.** It is not warranted to be fit
for any particular purpose, and you are solely responsible for how you use it
and for anything that results. If you are not comfortable restoring your Home
Assistant installation from a backup, do not run it.

## The problem

Deleting an integration does not erase its registry history. Entities become
tombstones in `core.entity_registry` → `deleted_entities` (keyed by
`(domain, platform, unique_id)`) and devices in `core.device_registry` →
`deleted_devices`. On reinstall, `EntityRegistry.async_get_or_create` pops the
matching tombstone and restores its `entity_id`, `name`, `icon`, `aliases`,
`area_id`, `categories` and `options` onto the new entity:

```python
# homeassistant/helpers/entity_registry.py
deleted_entity = self.deleted_entities.pop((domain, platform, unique_id), None)
...
# Restore entity_id if it's available
if self._entity_id_available(deleted_entity.entity_id):
    entity_id = deleted_entity.entity_id
```

Home Assistant only purges tombstones after `ORPHANED_ENTITY_KEEP_SECONDS`
(30 days). Because unique_ids are usually stable across reinstalls, a fresh
config entry asking for a different naming scheme still inherits the previous
entity_ids. Purging the tombstones is what breaks that inheritance.

### Or wait 30 days

Tombstones do expire on their own. A daily cleanup
(`CLEANUP_INTERVAL = 3600 * 24`) drops entity tombstones 30 days after they were
orphaned, and device tombstones on the same schedule
(`ORPHANED_ENTITY_KEEP_SECONDS`, `ORPHANED_DEVICE_KEEP_SECONDS`). So for the
naming problem, this tool buys you **immediacy, not capability** — everything it
removes from the registries would eventually go by itself.

The catch is that waiting and reinstalling are mutually exclusive. Reinstalling
*consumes* the tombstone: `async_get_or_create` pops it and hands the old
entity_id straight back, which restarts nothing and fixes nothing. The 30-day
clock only helps if you leave the integration uninstalled for a month. If you
want to reinstall today and have it name things the way the new config entry
asks, the tombstones have to go now.

Two things qualify that:

- **Only orphaned tombstones expire.** One whose config entry still exists has
  no `orphaned_timestamp` and is kept indefinitely. This tool does not touch
  those either, so nothing changes there.
- **Some leftovers never expire at all.** `.storage/<domain>*` files and
  long-term statistics are kept forever, with no cleanup of any kind behind
  them. `purge_storage` and `purge_statistics` are the only way those go, at any
  point. (`core.restore_state` rows are the exception; they lapse after 7 days.)

## scripts/pyscript/integration_registry_cleanup.py

The integration to purge is an argument; nothing in the script is specific to
one. It registers two actions:

| action | what it does |
| --- | --- |
| `pyscript.integration_registry_scan` | read-only; lists what each domain has left behind and builds the dropdown below |
| `pyscript.integration_registry_cleanup` | removes it |

### Prerequisites: pyscript, from HACS

The script runs inside the [pyscript](https://github.com/custom-components/pyscript)
custom integration, which is in the HACS default store — no custom repository
needed for it:

1. **HACS** → **Integrations** → search **Pyscript** → **Download**.
2. Restart Home Assistant.
3. Add to `configuration.yaml`:

   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```

   `allow_all_imports` is required — the script imports the registry helpers
   from `homeassistant.helpers`. `hass_is_global` gives it the `hass` object.
4. Restart Home Assistant again so the configuration takes effect.

### Install the script

Copy the one file into `/config/pyscript/`, whichever way suits your setup:

```sh
# from a clone of this repository, over SSH
scp scripts/pyscript/integration_registry_cleanup.py <host>:/config/pyscript/
```

```sh
# or on the Home Assistant host itself (SSH / Terminal add-on)
curl -fsSL -o /config/pyscript/integration_registry_cleanup.py \
  https://raw.githubusercontent.com/LegoTypes/ha-registry-clean/main/scripts/pyscript/integration_registry_cleanup.py
```

With the **Studio Code Server** or **File editor** add-on, or a Samba share,
create `/config/pyscript/integration_registry_cleanup.py` and paste the file in.

pyscript loads the file on **upload** — a bare `touch` is not enough, and no
restart is needed. Both actions then appear under **Developer Tools → Actions**;
search for *Integration registry*.

### What counts as removable

Nothing is decided per domain. Core records the owning config entry on every
registry entry and clears it when that entry is deleted, so `config_entry_id`
answers "does a configuration still own this?" — core says so itself:

```python
class DeletedDeviceEntry:
    # config_entry_id is None for orphaned deleted devices, i.e. devices whose
    # owning config entry has been removed
    config_entry_id: str | None = attr.ib()
```

| | `config_entry_id` | action |
| --- | --- | --- |
| tombstone | `None`, or an entry that no longer exists | **purge** — orphaned |
| tombstone | an entry that exists | keep — a live configuration owns it |
| live entity | an entry that no longer exists | **purge** — stale |
| live entity | `None` | **never touched** — this is how YAML integrations register (`person`) |
| device | no surviving entry | **purge** |

Two consequences worth knowing:

- **One deleted device can be cleaned while others keep running.** An
  integration with two panels configured, one of them deleted, leaves only the
  deleted one's entries ownerless. There is no "delete the integration first"
  step and no domain-wide refusal.
- **Sub-devices need no special handling.** A device belongs to exactly one
  config entry (`config_entries` is now a deprecated shim around
  `config_entry_id`), and `via_device_id` links a sub-device to its parent
  without affecting ownership. A sub-device of a live entry is protected; a
  sub-device of a deleted one orphans along with its parent.

### Scan and the domain dropdown

`pyscript.integration_registry_scan` buckets every ownerless registry entry by
the integration that owns it, then rewrites the `domain` field of the cleanup
action into a dropdown of what it found:

```
span_panel - 216 entity tombstones, 2 device tombstones, 3 storage files
```

| field | default | meaning |
| --- | --- | --- |
| `refresh_selector` | `true` | rebuild the dropdown, and blank the checkbox lists a scan makes stale |
| `notification` | `true` | raise a persistent notification with the table |

Options are ordered by how much is left behind, worst first. **The dropdown is
searchable** — type to filter it. That comes from `custom_value`:
`ha-selector-select` renders the filtering `ha-generic-picker` only when
`custom_value` is set, and a plain unfiltered `ha-select` otherwise.
`custom_value` also means you can type a domain the scan did not offer.

When nothing qualifies, the field stays a plain text box and says so:

> Nothing needs cleaning: every registry entry still belongs to a configuration
> that exists, so there are no orphans to list. Delete an integration — or one
> config entry of one — and its leftovers appear here on their own. Home
> Assistant also drops entity tombstones by itself 30 days after the entity was
> removed.

The scan runs at startup, after any applied cleanup, and **whenever entities or
devices are removed from the registry** — so deleting an integration updates the
dropdown by itself, with no reload and nothing to remember. Removals arrive one
per entity, so the rescan is debounced: it fires about five seconds after the
last one.

An open Developer Tools page updates too. Core has no "service description
changed" event — the frontend refetches when it sees `service_registered`, which
only `hass.services.async_register` fires — so after publishing a changed
description the cleanup service is re-registered with its own existing handler,
making that event true rather than fabricated. This happens only when the
options actually change, since it costs every connected frontend a refetch.

### Cleanup

Call it from **Developer Tools → Actions**.

| field | default | meaning |
| --- | --- | --- |
| `domain` | *(required)* | integration domain to purge; searchable dropdown built by the scan |
| `dry_run` | `true` | report only, and fill in the checkboxes below |
| `entity_filter` | — | entity_id glob narrowing the candidates, e.g. `*_energy_*` |
| `entities` | *(filled by the dry run)* | entities to purge, every box ticked |
| `devices` | *(filled by the dry run)* | devices to purge, likewise |
| `purge_storage` | `false` | also remove the integration's `.storage` files and its `core.restore_state` rows |
| `purge_statistics` | `false` | also clear long-term statistics for the purged entity_ids |
| `backup` | `true` | save everything it touches under `/config/registry_backups/` first |

**The flow is dry run, then apply.** A dry run reports what it found *and*
republishes `entities` and `devices` as checkbox lists holding exactly that,
every box ticked — so the common case needs no clicking. Untick anything you
want to keep, set `dry_run: false`, and run it again.

`entity_filter` narrows what the dry run offers rather than what it hides:
anything the glob excludes never appears as a checkbox and is never touched. Use
it to cut a long list down before unticking within it.

```yaml
action: pyscript.integration_registry_cleanup
data:
  domain: some_integration
  dry_run: false
  purge_storage: true
  purge_statistics: true
```

The selection is a **filter, not a work order**: the apply pass recomputes the
orphans and purges the intersection, so a list left sitting in an open tab can
never remove something that has since become owned again. The checkboxes are
published server-side and shared by every tab, so they are stamped with the
domain they were built for; applying them against a different domain aborts.

It returns a response with the full lists and logs a summary. A persistent
notification is raised only when a run actually removes something — a dry run is
already reported by its response and by the checkboxes it fills in, and a run
that removes nothing is not news. Re-running is a no-op.

### Statistics

States expire under the recorder's retention (10 days by default), but
long-term statistics are kept forever, so a deleted integration's statistics sit
in the recorder database indefinitely and show up in **Developer Tools →
Statistics** as no longer being recorded. `purge_statistics` clears them for the
entity_ids being purged, via `list_statistic_ids` and the recorder's
`async_clear_statistics`.

One guard matters: **entity ids get recycled**. An id that something currently
uses is never cleared, because a working entity that inherited a purged
tombstone's entity_id would otherwise lose its history. Statistics are also
*not* covered by the backup below — they live in the recorder database, not in
`.storage`.

### Backup and recovery

With `backup: true` (the default), everything the run touches goes into one
flat, timestamped directory before anything is changed:

```
/config/registry_backups/<domain>_<YYYYmmdd_HHMMSS>/
├── core.entity_registry     copied
├── core.device_registry     copied
├── core.restore_state       copied
└── <domain>*                moved — the backup is the only remaining copy
```

`.storage` files are picked per config entry, not per domain: a file naming an
entry id that still exists is never touched, and a shared file with no entry id
in its name (`<domain>_settings`) is kept while any config entry for the
integration remains.

The path is in the action's response (`backup_dir`), the log summary, and the
persistent notification, alongside the undo instructions.

To undo a run:

```sh
ha core stop
cp /config/registry_backups/<domain>_<timestamp>/* /config/.storage/
ha core start
```

Core must be stopped for this. Home Assistant holds the registries in memory and
flushes them to `.storage` on a timer, so files copied back under a running core
are overwritten within seconds — the same reason the action edits the registries
through the registry APIs rather than by writing JSON.

With `backup: false` there is no recovery path: purged `.storage` files are
deleted outright and the registry edits are saved in place. A full Home Assistant
backup taken beforehand is the only way back.

### Design notes

- The registries are edited **in memory** through `entity_registry.async_get` /
  `device_registry.async_get`. Editing the `.storage` JSON directly does not
  work: Home Assistant overwrites it on the next flush of its in-memory copy.
- Removing a live orphan creates a tombstone of its own, so the tombstone sweep
  runs against a freshly collected list rather than the one gathered for the
  report.
- The dropdown and the checkbox lists are published with
  `async_set_service_schema`, the same helper pyscript itself calls to publish a
  docstring's YAML. The already-registered description is read back with
  `async_get_cached_service_description` and edited, so the docstring stays the
  single source of truth and the declared selectors can be restored.
- `task.executor` accepts only real Python functions, never pyscript-defined
  ones, so blocking work is handed to stdlib callables one call at a time
  (`os.listdir`, `shutil.copy2`, `shutil.move`, `os.remove`), plus
  `list_statistic_ids`, which queries the recorder database.
- `DeviceRegistry.devices` and `.deleted_devices` became deprecation-reporting
  views in 2026.9, so `_devices` / `_deleted_devices` are preferred with a
  fallback for older cores.
- `RestoreStateData` has no scheduled save; entries are popped from
  `last_states` and `async_dump_states()` rewrites the file.
- `STORAGE_KEEP` guards the `.storage` prefix match so `core.*`, `hacs.*` and
  `lovelace*` files are never claimed by a domain name.
- The cleanup's persistent notification id is per-domain, so cleaning two
  integrations leaves two notifications rather than one overwriting the other.

### Verified against HA 2026.8.3

A synthetic domain seeded with three orphaned tombstones plus one tombstone
owned by a live config entry:

| step | result |
| --- | --- |
| dry run | offered the 3 orphans; the owned tombstone excluded |
| checkbox field | `multiple: true`, `mode: list`, all 3 options ticked by default |
| `entity_filter: *orphan_2*` | published exactly one checkbox |
| apply with one entity selected | purged that one; the other two and the owned one remained |
| apply with no selection | purged the remaining two; the owned tombstone still protected |

An earlier whole-domain run, on a real integration with a large backlog:

| | before | after |
| --- | --- | --- |
| `deleted_entities` (target domain) | 229 | 0 |
| `deleted_devices` (target domain) | 2 | 0 |
| `core.restore_state` rows | 75 | 0 |
| `.storage/<domain>_*` files | 3 | 0 |
| live entities / devices (all integrations) | 59 / 14 | 59 / 14 |

## License

MIT — see [LICENSE](LICENSE).
