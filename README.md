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

## scripts/pyscript/integration_registry_cleanup.py

The integration to purge is an argument; nothing in the script is specific to
one. It registers two actions:

| action | what it does |
| --- | --- |
| `pyscript.integration_registry_scan` | read-only; lists what each domain has left behind and builds the dropdown below |
| `pyscript.integration_registry_cleanup` | removes it |

### Install

```sh
scp scripts/pyscript/integration_registry_cleanup.py <host>:/config/pyscript/
```

pyscript hot-reloads on file **upload** (a bare `touch` is not enough), so no
restart is needed once pyscript itself is configured:

```yaml
# configuration.yaml -- needs one core restart to take effect
pyscript:
  allow_all_imports: true
  hass_is_global: true
```

`allow_all_imports` is required: the script imports the registry helpers from
`homeassistant.helpers`. `hass_is_global` gives it the `hass` object.

### Scan and the domain dropdown

`pyscript.integration_registry_scan` buckets every registry entry — live and
tombstoned, entities and devices, plus matching `.storage` files — by the
integration that owns it, then rewrites the `domain` field of the cleanup action
into a dropdown of what it found:

```
span_panel - 229 entity tombstones, 2 device tombstones, 3 storage files
```

| field | default | meaning |
| --- | --- | --- |
| `refresh_selector` | `true` | rebuild the cleanup action's dropdown |
| `notification` | `true` | raise a persistent notification with the table |

**Only domains with tombstones are offered.** A domain with live entries and no
tombstones is an integration that is working fine — `person` and the other
YAML-configured core integrations land there, and offering them would invite
someone to purge a running integration. Domains that still have a config entry
are listed separately as "still configured", because the cleanup refuses to run
on those anyway. Options are ordered by how much is left behind, worst first.

**The dropdown is searchable** — type to filter it. That comes from
`custom_value`: `ha-selector-select` renders the filtering `ha-generic-picker`
only when `custom_value` is set, and a plain unfiltered `ha-select` otherwise.
`custom_value` also means you can still type a domain the scan did not offer.

The scan runs on `@time_trigger("startup")`, so every pyscript reload and every
Home Assistant restart refreshes the list, and again after any applied cleanup.

**Refreshing an open page.** Core has no "service description changed" event —
the frontend refetches descriptions when it sees `service_registered` /
`service_removed`, which only `hass.services.async_register` fires. Updating a
description does not fire anything, so a browser tab that is already open keeps
the old options until you reload the page.

### Cleanup

Call it from **Developer Tools → Actions**. Delete the integration's config
entry first — the action refuses to run while one exists.

| field | default | meaning |
| --- | --- | --- |
| `domain` | *(required)* | integration domain to purge; searchable dropdown built by the scan |
| `dry_run` | `true` | report only, change nothing |
| `purge_storage` | `false` | also remove `.storage/<domain>*` and the domain's `core.restore_state` rows |
| `backup` | `true` | save everything it touches under `/config/registry_backups/` first (see below) |

```yaml
action: pyscript.integration_registry_cleanup
data:
  domain: some_integration
  dry_run: false
  purge_storage: true
```

It returns a response with the full lists, logs a summary, and raises a
persistent notification. Re-running is a no-op.

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
- Live entities and devices are removed before the tombstone sweep, because
  `async_remove` / `async_remove_device` create tombstones of their own.
- The dropdown is published with `async_set_service_schema`, the same helper
  pyscript itself calls to publish a docstring's YAML. The already-registered
  description is read back with `async_get_cached_service_description` and
  edited, so the docstring stays the single source of truth and the original
  text selector can be restored when a scan finds nothing to offer.
- `task.executor` accepts only real Python functions, never pyscript-defined
  ones, so blocking work is handed to stdlib callables one call at a time
  (`os.listdir`, `glob.glob`, `shutil.copy2`, `shutil.move`, `os.remove`).
- `DeviceRegistry.devices` and `.deleted_devices` became deprecation-reporting
  views in 2026.9, so `_devices` / `_deleted_devices` are preferred with a
  fallback for older cores. `DeletedDeviceEntry` carries an explicit `domain`;
  `DeviceEntry` does not, hence the `getattr` in `_device_domains`.
- `RestoreStateData` has no scheduled save; entries are popped from
  `last_states` and `async_dump_states()` rewrites the file.
- `STORAGE_KEEP` guards the `.storage` prefix match so `core.*`, `hacs.*` and
  `lovelace*` files are never claimed by a domain name.
- The cleanup's persistent notification id is per-domain, so cleaning two
  integrations leaves two notifications rather than one overwriting the other.

### Verified against HA 2026.8.3

Cleanup of an integration with a large tombstone backlog:

| | before | after |
| --- | --- | --- |
| `deleted_entities` (target domain) | 229 | 0 |
| `deleted_devices` (target domain) | 2 | 0 |
| `core.restore_state` rows | 75 | 0 |
| `.storage/<domain>_*` files | 3 | 0 |
| live entities / devices (all integrations) | 59 / 14 | 59 / 14 |

Scan and dropdown: a synthetic tombstone under a throwaway domain appeared as
`zz_probe_domain - 1 entity tombstone` with `custom_value: True`; cleaning it
emptied the candidate list and the `domain` field reverted to its declared text
selector. `person` (live entities, no tombstones) was correctly not offered.

## License

MIT — see [LICENSE](LICENSE).
