"""Purge the registry entries an integration leaves behind when it is deleted.

Removing an integration does not erase its registry history.  Entities become
tombstones in ``core.entity_registry`` -> ``deleted_entities`` and devices
become tombstones in ``core.device_registry`` -> ``deleted_devices``.  Home
Assistant keeps them for ORPHANED_ENTITY_KEEP_SECONDS (30 days), and when an
entity with a matching (domain, platform, unique_id) reappears,
``async_get_or_create`` restores the tombstone's entity_id, name, icon, aliases,
area and options onto the new entity.  A reinstall therefore inherits the old
naming scheme no matter what the new config entry asks for.

What counts as removable is decided per entry, never per domain: core records
the owning config entry on every registry entry and clears it when that entry is
deleted, so ``config_entry_id is None`` (or an id that no longer exists) means
"no configuration owns this".  Core says so itself on DeletedDeviceEntry:

    # config_entry_id is None for orphaned deleted devices, i.e. devices whose
    # owning config entry has been removed

That makes it safe to clean one deleted panel while another panel of the same
integration keeps running, and it covers sub-devices for free -- a device
belongs to exactly one config entry, and via_device_id does not affect
ownership.

The one exception is a *live* entity with no config entry at all: that is how
YAML-configured integrations register (``person`` and friends), so live entries
without an owner are never touched.

Two services are registered.

pyscript.integration_registry_scan   (read-only)

  Buckets orphaned registry entries by the integration that owns them and
  rebuilds the ``domain`` dropdown on the cleanup action.  Runs at startup,
  after any applied cleanup, and whenever registry entries are removed.

pyscript.integration_registry_cleanup

  domain           integration domain to purge             (required)
  dry_run          report only, and publish the checkboxes (default: true)
  filter           entity_id glob narrowing the candidates
  entities         entity_ids to purge; the dry run fills these in
  devices          device ids to purge; likewise
  purge_storage    also remove the integration's .storage files and its
                   core.restore_state entries               (default: false)
  purge_statistics also clear long-term statistics for the purged entity_ids
                                                            (default: false)
  backup           copy the registries aside first          (default: true)

A dry run republishes ``entities`` and ``devices`` as checkbox lists holding
exactly what it found, every box ticked.  Untick what you want to keep, set
dry_run to false, and run it again.  The selection is a filter, not a work
order: the apply pass recomputes the orphans and purges the intersection, so a
stale list can never remove something that has since become owned again.

With ``backup`` on, everything the cleanup touches is copied to

    /config/registry_backups/<domain>_<YYYYmmdd_HHMMSS>/

as a flat directory: core.entity_registry, core.device_registry and
core.restore_state are copied there, and any purged .storage files are *moved*
there rather than deleted.  To undo a run, stop Home Assistant, copy every file
from that directory back into /config/.storage/, and start it again -- the
registries must not be written while core is running, or the in-memory copy will
simply overwrite them at the next flush.  Cleared statistics are not covered by
that backup; they live in the recorder database.

For the same reason the registries are edited in memory through the registry
APIs here, never by writing .storage directly.
"""

import copy
import fnmatch
import glob
import os
import shutil
import time

from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    restore_state as rs,
)
from homeassistant.helpers.service import (
    async_get_cached_service_description,
    async_set_service_schema,
)

try:
    from homeassistant.components.recorder import get_instance as _recorder_instance
    from homeassistant.components.recorder.statistics import list_statistic_ids
except Exception:
    # Recorder is a default integration, but the script still loads without it;
    # purge_statistics reports itself unavailable instead of failing to import.
    _recorder_instance = None
    list_statistic_ids = None

PYSCRIPT_DOMAIN = "pyscript"
CLEANUP_SERVICE = "integration_registry_cleanup"
SCAN_SERVICE = "integration_registry_scan"
BACKUP_ROOT = "registry_backups"

# Deleting an integration removes its entities one at a time, so a rescan is
# debounced under a single task name: each new removal kills the pending one.
RESCAN_TASK = "integration_registry_rescan"
RESCAN_DEBOUNCE = 5

# .storage prefixes that belong to Home Assistant or HACS even when they match
# the domain glob -- never touch these.
STORAGE_KEEP = ("core.", "hacs.", "lovelace")

# Registry files copied into the backup directory before anything is changed.
REGISTRY_FILES = ("core.entity_registry", "core.device_registry", "core.restore_state")

# Config entry ids are 26-character ULIDs.  A .storage file whose name ends in
# one belongs to that entry alone; a file without one is shared by the whole
# integration.
ENTRY_ID_LENGTH = 26

# The selectors and descriptions the cleanup action declares in its own
# docstring, captured the first time they are rewritten so they can be restored.
# Reset when pyscript reloads this file.
_ORIGINAL_FIELDS = {}

# Which domain the published `entities` / `devices` checkboxes were built for.
_SELECTION_DOMAIN = None


def _live_entry_ids():
    """Ids of every config entry that currently exists.

    Disabled and ignored entries count: they still own their registry entries.
    """
    return set([entry.entry_id for entry in hass.config_entries.async_entries()])


def _device_container(dev_reg):
    """Live device container.

    `DeviceRegistry.devices` became a deprecation-reporting view in 2026.9, so
    prefer the private attribute and fall back for older cores.
    """
    return getattr(dev_reg, "_devices", None) or dev_reg.devices


def _deleted_device_container(dev_reg):
    """Tombstoned device container, same deprecation story as above."""
    return getattr(dev_reg, "_deleted_devices", None) or dev_reg.deleted_devices


def _device_entry_id(device):
    """The config entry a device belongs to.

    A device now belongs to exactly one config entry; `config_entries` is a
    deprecated shim around it.  Older cores only have the set.
    """
    entry_id = getattr(device, "config_entry_id", None)
    if entry_id is not None:
        return entry_id
    entries = getattr(device, "config_entries", None) or set()
    for candidate in entries:
        return candidate
    return None


def _device_domains(device):
    """Every integration domain a device (live or tombstoned) belongs to."""
    domains = set()
    for identifier in device.identifiers:
        if len(identifier):
            domains.add(identifier[0])
    # DeletedDeviceEntry records the owning integration explicitly.
    explicit = getattr(device, "domain", None)
    if explicit:
        domains.add(explicit)
    return domains


def _device_label(device):
    """Readable device name for a checkbox or a report line."""
    name = getattr(device, "name_by_user", None) or getattr(device, "name", None)
    identifiers = sorted([f"{pair[0]}/{pair[1]}" for pair in device.identifiers if len(pair) > 1])
    if name and identifiers:
        return f"{name} ({identifiers[0]})"
    if identifiers:
        return identifiers[0]
    return name or device.id


def _is_orphan(entry_id, live_entry_ids):
    """True when nothing that currently exists owns this registry entry."""
    return entry_id is None or entry_id not in live_entry_ids


def _matches(text, pattern):
    """Glob match, with an empty pattern meaning 'everything'."""
    if not pattern:
        return True
    return fnmatch.fnmatch(text, pattern)


def _collect(domain, pattern, live_entry_ids):
    """Everything for `domain` that no surviving config entry owns.

    Returns (live_entities, dead_entities, live_devices, dead_devices) where the
    entity lists hold registry objects and the device lists hold (key, device)
    pairs -- tombstones are popped by key.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    live_entities = [
        entry
        for entry in list(ent_reg.entities.values())
        if entry.platform == domain
        # A live entity with no owner at all is how YAML integrations register.
        # Only a reference to an entry that has vanished makes one an orphan.
        and entry.config_entry_id is not None
        and entry.config_entry_id not in live_entry_ids
        and _matches(entry.entity_id, pattern)
    ]

    dead_entities = [
        (key, entry)
        for key, entry in list(ent_reg.deleted_entities.items())
        if getattr(entry, "platform", None) == domain
        and _is_orphan(getattr(entry, "config_entry_id", None), live_entry_ids)
        and _matches(entry.entity_id, pattern)
    ]

    live_devices = [
        device
        for device in list(_device_container(dev_reg).values())
        if domain in _device_domains(device)
        and _device_entry_id(device) is not None
        and _device_entry_id(device) not in live_entry_ids
    ]

    dead_devices = [
        (key, device)
        for key, device in list(_deleted_device_container(dev_reg).items())
        if domain in _device_domains(device)
        and _is_orphan(_device_entry_id(device), live_entry_ids)
    ]

    return live_entities, dead_entities, live_devices, dead_devices


def _storage_entry_id(name):
    """The config entry id embedded in a .storage file name, if any."""
    tail = name.rsplit(".", 1)[-1]
    if len(tail) == ENTRY_ID_LENGTH and tail.isalnum():
        return tail
    return None


def _storage_candidates(domain, names, storage_dir, live_entry_ids, domain_has_live_entry):
    """.storage files this integration owns that no live config entry needs.

    Per-entry files name their entry id; a file without one is shared by the
    whole integration, so it is only removable once nothing of the integration
    is configured any more.
    """
    matched = []
    for name in names:
        if not name.startswith(domain) or name.startswith(STORAGE_KEEP):
            continue
        if not os.path.isfile(os.path.join(storage_dir, name)):
            continue
        entry_id = _storage_entry_id(name)
        if entry_id is not None and entry_id in live_entry_ids:
            continue
        if domain_has_live_entry and entry_id is None:
            continue
        matched.append(name)
    return sorted(matched)


def _statistic_ids_for(entity_ids):
    """Long-term statistics belonging to entity_ids that nothing uses now.

    Entity ids are recycled, so an id that something currently owns is left
    alone -- clearing it would destroy a working entity's history.
    """
    if list_statistic_ids is None or _recorder_instance is None:
        return None
    if "recorder" not in hass.config.components:
        return None
    ent_reg = er.async_get(hass)
    in_use = set(ent_reg.entities.keys())
    candidates = set(
        [
            entity_id
            for entity_id in entity_ids
            if entity_id not in in_use and hass.states.get(entity_id) is None
        ]
    )
    if not candidates:
        return []
    rows = task.executor(list_statistic_ids, hass, candidates)
    return sorted([row["statistic_id"] for row in rows])


def _empty_dropdown_help():
    """What the `domain` field says when there is no dropdown to show."""
    return (
        "Integration domain to purge, as it appears in its manifest."
        " Nothing needs cleaning: every registry entry still belongs to a"
        " configuration that exists, so there are no orphans to list."
        " Delete an integration - or one config entry of one - and its leftovers"
        " appear here on their own."
        " Home Assistant also drops entity tombstones by itself 30 days after"
        " the entity was removed."
    )


def _field_options(pairs):
    """[(value, label)] -> the option dicts a select selector wants."""
    return [{"value": value, "label": label} for value, label in pairs]


def _apply_selection_field(field, original, options, domain, noun):
    """Turn one field into a pre-ticked checkbox list, or back into a blank."""
    if options:
        field["selector"] = {
            "select": {"multiple": True, "mode": "list", "options": options}
        }
        # Every box ticked: the common case is "purge all of it", and nobody
        # should have to click 216 checkboxes to get there.
        field["default"] = [option["value"] for option in options]
        field["description"] = (
            f"{len(options)} orphaned {noun} found for {domain}, all ticked."
            " Untick anything you want to keep, then run again with dry_run off."
        )
    else:
        field["selector"] = {
            "select": {"multiple": True, "mode": "list", "options": []}
        }
        field.pop("default", None)
        field["description"] = original.get("description")


def _publish(domain_options, selection):
    """Rewrite the cleanup action's fields in one pass.

    `selection` is None to blank the checkbox lists, or
    {"domain": str, "entities": [...], "devices": [...]} to fill them in.

    async_set_service_schema is the same call pyscript makes to publish a
    service's docstring YAML, and the description already registered is read
    back and edited, which keeps the docstring the single source of truth.
    """
    global _SELECTION_DOMAIN

    published = async_get_cached_service_description(
        hass, PYSCRIPT_DOMAIN, CLEANUP_SERVICE
    )
    if not published:
        log.warning(
            f"{SCAN_SERVICE}: {PYSCRIPT_DOMAIN}.{CLEANUP_SERVICE} has no published "
            "description yet; leaving its fields alone"
        )
        return False, False

    description = copy.deepcopy(published)
    fields = description.get("fields", {})
    for name in ("domain", "entities", "devices"):
        if name not in fields:
            log.warning(f"{SCAN_SERVICE}: {CLEANUP_SERVICE} has no '{name}' field")
            return False, False
        if name not in _ORIGINAL_FIELDS:
            _ORIGINAL_FIELDS[name] = copy.deepcopy(fields[name])

    domain_field = fields["domain"]
    if domain_options:
        # custom_value keeps the field usable for a domain the scan cannot see,
        # and it is what makes the field searchable -- ha-selector-select only
        # renders the filtering ha-generic-picker when custom_value is set.
        # Options stay in scan order (most leftovers first), so no `sort` here.
        domain_field["selector"] = {
            "select": {
                "mode": "dropdown",
                "custom_value": True,
                "options": domain_options,
            }
        }
        domain_field["description"] = _ORIGINAL_FIELDS["domain"].get("description")
    else:
        domain_field["selector"] = copy.deepcopy(
            _ORIGINAL_FIELDS["domain"].get("selector")
        )
        domain_field["description"] = _empty_dropdown_help()

    _apply_selection_field(
        fields["entities"],
        _ORIGINAL_FIELDS["entities"],
        selection["entities"] if selection else [],
        selection["domain"] if selection else None,
        "entities",
    )
    _apply_selection_field(
        fields["devices"],
        _ORIGINAL_FIELDS["devices"],
        selection["devices"] if selection else [],
        selection["domain"] if selection else None,
        "devices",
    )

    _SELECTION_DOMAIN = selection["domain"] if selection else None

    if description == published:
        # Already published exactly this; re-registering would make every
        # connected frontend refetch the whole service description set for
        # nothing.
        return True, False

    async_set_service_schema(hass, PYSCRIPT_DOMAIN, CLEANUP_SERVICE, description)
    _republish_service()
    return True, True


def _republish_service():
    """Make open pages pick up the new description.

    Core fires no event when a description changes; the frontend refetches when
    a service is registered.  Re-registering the cleanup service with its own
    existing handler makes that event true rather than fabricated.
    `ServiceRegistry._async_register` only swaps the Service object and fires
    the event -- it does not touch the description cache, so the schema
    published just above survives.
    """
    existing = hass.services.async_services_for_domain(PYSCRIPT_DOMAIN).get(
        CLEANUP_SERVICE
    )
    if existing is None:
        log.warning(f"{SCAN_SERVICE}: {CLEANUP_SERVICE} is not registered")
        return False
    hass.services.async_register(
        PYSCRIPT_DOMAIN,
        CLEANUP_SERVICE,
        existing.job.target,
        existing.schema,
        existing.supports_response,
        existing.job.job_type,
        description_placeholders=existing.description_placeholders,
    )
    return True


def _empty_counts():
    return {
        "live_entities": 0,
        "deleted_entities": 0,
        "live_devices": 0,
        "deleted_devices": 0,
        "storage_files": 0,
    }


def _bucket(found, domain):
    """Per-domain tally, created on first sight."""
    if domain not in found:
        found[domain] = _empty_counts()
    return found[domain]


LABEL_PARTS = (
    ("deleted_entities", "entity tombstone", "entity tombstones"),
    ("deleted_devices", "device tombstone", "device tombstones"),
    ("live_entities", "stale entity", "stale entities"),
    ("live_devices", "stale device", "stale devices"),
    ("storage_files", "storage file", "storage files"),
)


def _label(domain, counts):
    """Dropdown label: the domain plus what is left without an owner."""
    parts = []
    for key, one, many in LABEL_PARTS:
        count = counts[key]
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    return f"{domain} - {', '.join(parts)}"


def _as_list(value):
    """A service field that may arrive as None, one string, or a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return list(value)


def _scan(refresh_selector, selection=None):
    """Bucket every ownerless registry entry by the integration that owns it."""
    live_entry_ids = _live_entry_ids()
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    storage_dir = hass.config.path(".storage")
    storage_names = task.executor(os.listdir, storage_dir)

    found = {}

    for entry in list(ent_reg.entities.values()):
        # Live entries with no owner at all are YAML-registered, not orphaned.
        if (
            entry.config_entry_id is not None
            and entry.config_entry_id not in live_entry_ids
        ):
            _bucket(found, entry.platform)["live_entities"] += 1

    for entry in list(ent_reg.deleted_entities.values()):
        platform = getattr(entry, "platform", None)
        if platform and _is_orphan(
            getattr(entry, "config_entry_id", None), live_entry_ids
        ):
            _bucket(found, platform)["deleted_entities"] += 1

    for device in list(_device_container(dev_reg).values()):
        entry_id = _device_entry_id(device)
        if entry_id is not None and entry_id not in live_entry_ids:
            for domain in _device_domains(device):
                _bucket(found, domain)["live_devices"] += 1

    for device in list(_deleted_device_container(dev_reg).values()):
        if _is_orphan(_device_entry_id(device), live_entry_ids):
            for domain in _device_domains(device):
                _bucket(found, domain)["deleted_devices"] += 1

    for domain in list(found):
        found[domain]["storage_files"] = len(
            _storage_candidates(
                domain,
                storage_names,
                storage_dir,
                live_entry_ids,
                bool(hass.config_entries.async_entries(domain)),
            )
        )

    # Negative total sorts the biggest offender first, then by name.
    ranked = sorted([(-sum(found[domain].values()), domain) for domain in found])
    options = _field_options(
        [(domain, _label(domain, found[domain])) for _rank, domain in ranked]
    )

    result = {
        "domains": found,
        "candidates": [option["value"] for option in options],
        "selector_refreshed": False,
        "selector_changed": False,
    }
    if refresh_selector:
        refreshed, changed = _publish(options, selection)
        result["selector_refreshed"] = refreshed
        result["selector_changed"] = changed
    return result


@service(supports_response="optional")
def integration_registry_scan(refresh_selector=True, notification=True):
    """yaml
name: Integration registry scan
description: >-
  List every integration domain that still owns registry entries no config entry
  backs, and refresh the domain dropdown on the integration registry cleanup
  action.
fields:
  refresh_selector:
    description: >-
      Rebuild the domain dropdown on pyscript.integration_registry_cleanup from
      what this scan finds, and blank its entity and device checkbox lists,
      which a scan makes stale.
    default: true
    selector:
      boolean:
  notification:
    description: Raise a persistent notification with the results.
    default: true
    selector:
      boolean:
"""
    refresh_selector = bool(refresh_selector)
    notification = bool(notification)

    result = _scan(refresh_selector)

    log.info(
        f"{SCAN_SERVICE}: {len(result['candidates'])} domain(s) with orphaned "
        f"registry entries, selector_changed={result['selector_changed']}"
    )
    if notification:
        _notify_scan(result)
    return result


@service(supports_response="optional")
def integration_registry_cleanup(
    domain=None,
    dry_run=True,
    entity_filter=None,
    entities=None,
    devices=None,
    purge_storage=False,
    purge_statistics=False,
    backup=True,
):
    """yaml
name: Integration registry cleanup
description: >-
  Remove the entity and device registry entries an integration left behind,
  including the deleted_entities / deleted_devices tombstones that make a
  reinstall inherit its old entity_ids and names. Only entries no existing
  config entry owns are ever touched, so one deleted device of an integration
  can be cleaned while its other devices keep running.
fields:
  domain:
    description: >-
      Integration domain to purge, as it appears in its manifest. The dropdown
      is built by pyscript.integration_registry_scan and lists what each domain
      has left behind, most first; type to filter it.
    example: span_panel
    required: true
    selector:
      text:
  dry_run:
    description: >-
      Report what would be removed without changing anything, and fill in the
      entity and device checkboxes below with what was found.
    default: true
    selector:
      boolean:
  entity_filter:
    description: >-
      Optional entity_id glob narrowing the candidates, e.g. sensor.span_panel_*
      or *_energy_*. A dry run only offers checkboxes for what matches, so
      anything filtered out is left alone.
    example: "*_energy_*"
    selector:
      text:
  entities:
    description: >-
      Entities to purge. A dry run fills this in with everything it found, every
      box ticked; untick what you want to keep.
    selector:
      select:
        multiple: true
        mode: list
        options: []
  devices:
    description: >-
      Devices to purge. A dry run fills this in the same way.
    selector:
      select:
        multiple: true
        mode: list
        options: []
  purge_storage:
    description: >-
      Also remove the integration's own .storage files and its entries in
      core.restore_state. A file naming a config entry that still exists is
      never touched, and a shared file with no entry id in its name is kept
      while any config entry for the integration remains.
    default: false
    selector:
      boolean:
  purge_statistics:
    description: >-
      Also clear long-term statistics for the purged entity_ids. States expire
      under the recorder's retention, but statistics are kept forever. An
      entity_id something currently uses is never cleared.
    default: false
    selector:
      boolean:
  backup:
    description: >-
      Copy core.entity_registry, core.device_registry and core.restore_state to
      /config/registry_backups/<domain>_<timestamp>/ before changing anything;
      purged .storage files are moved there instead of being deleted. To undo a
      run, stop Home Assistant, copy the files back into /config/.storage/ and
      start it again. Cleared statistics are not covered - they live in the
      recorder database.
    default: true
    selector:
      boolean:
"""
    domain = str(domain or "").strip()
    dry_run = bool(dry_run)
    entity_filter = str(entity_filter or "").strip()
    purge_storage = bool(purge_storage)
    purge_statistics = bool(purge_statistics)
    backup = bool(backup)
    chosen_entities = _as_list(entities)
    chosen_devices = _as_list(devices)

    storage_dir = hass.config.path(".storage")
    backup_dir = hass.config.path(
        BACKUP_ROOT, f"{domain}_{time.strftime('%Y%m%d_%H%M%S')}"
    )

    result = {
        "domain": domain,
        "dry_run": dry_run,
        "filter": entity_filter,
        "aborted": None,
        "backup_dir": None,
        "restore_with": None,
        "entities_removed": [],
        "deleted_entities_purged": [],
        "devices_removed": [],
        "deleted_devices_purged": [],
        "restore_state_purged": [],
        "storage_files_purged": [],
        "statistics_cleared": [],
        "statistics_note": None,
    }

    if not domain:
        result["aborted"] = "No domain given. Pass the integration domain to purge."
        log.error(f"{CLEANUP_SERVICE}: {result['aborted']}")
        _notify(result)
        return result

    # The checkbox lists are published server-side and shared by every tab, so a
    # selection made for one domain must never be applied to another.
    if (
        (chosen_entities or chosen_devices)
        and _SELECTION_DOMAIN is not None
        and _SELECTION_DOMAIN != domain
    ):
        result["aborted"] = (
            f"The entity and device checkboxes were filled in for "
            f"'{_SELECTION_DOMAIN}', not '{domain}'. Run a dry run for "
            f"'{domain}' first."
        )
        log.error(f"{CLEANUP_SERVICE}: {result['aborted']}")
        _notify(result)
        return result

    live_entry_ids = _live_entry_ids()
    domain_has_live_entry = bool(hass.config_entries.async_entries(domain))

    # Everything the domain has left without an owner, before any selection:
    # the checkboxes must offer the whole filtered set, not what a previous
    # selection narrowed it to.
    all_live_entities, all_dead_entities, all_live_devices, all_dead_devices = _collect(
        domain, entity_filter, live_entry_ids
    )

    entity_options = _field_options(
        [(entry.entity_id, f"{entry.entity_id}  (stale)") for entry in all_live_entities]
        + [
            (entry.entity_id, f"{entry.entity_id}  <-  {entry.unique_id}")
            for _key, entry in all_dead_entities
        ]
    )
    device_options = _field_options(
        [(device.id, _device_label(device)) for device in all_live_devices]
        + [(device.id, _device_label(device)) for _key, device in all_dead_devices]
    )

    live_entities = all_live_entities
    dead_entities = all_dead_entities
    live_devices = all_live_devices
    dead_devices = all_dead_devices
    if chosen_entities:
        wanted = set(chosen_entities)
        live_entities = [e for e in live_entities if e.entity_id in wanted]
        dead_entities = [(k, e) for k, e in dead_entities if e.entity_id in wanted]
    if chosen_devices:
        wanted = set(chosen_devices)
        live_devices = [d for d in live_devices if d.id in wanted]
        dead_devices = [(k, d) for k, d in dead_devices if d.id in wanted]

    result["entities_removed"] = sorted([entry.entity_id for entry in live_entities])
    result["deleted_entities_purged"] = sorted(
        [f"{entry.entity_id}  <-  {entry.unique_id}" for _key, entry in dead_entities]
    )
    result["devices_removed"] = sorted([_device_label(d) for d in live_devices])
    result["deleted_devices_purged"] = sorted(
        [_device_label(d) for _key, d in dead_devices]
    )

    owned_entity_ids = set(result["entities_removed"])
    for _key, entry in dead_entities:
        owned_entity_ids.add(entry.entity_id)

    restore_data = None
    storage_files = []
    if purge_storage:
        restore_data = rs.async_get(hass)
        result["restore_state_purged"] = sorted(
            [
                entity_id
                for entity_id in list(restore_data.last_states)
                if entity_id in owned_entity_ids
            ]
        )
        storage_files = _storage_candidates(
            domain,
            task.executor(os.listdir, storage_dir),
            storage_dir,
            live_entry_ids,
            domain_has_live_entry,
        )
        result["storage_files_purged"] = storage_files

    statistic_ids = []
    if purge_statistics:
        found = _statistic_ids_for(owned_entity_ids)
        if found is None:
            result["statistics_note"] = "recorder unavailable; statistics untouched"
        else:
            statistic_ids = found
            result["statistics_cleared"] = statistic_ids

    if dry_run:
        if backup:
            result["backup_dir"] = f"{backup_dir}  (would be created)"
            result["restore_with"] = _restore_hint("that directory")
        _scan(
            True,
            selection={
                "domain": domain,
                "entities": entity_options,
                "devices": device_options,
            },
        )
        log.info(_summary(result, "DRY RUN"))
        _notify(result)
        return result

    if backup:
        task.executor(os.makedirs, backup_dir, exist_ok=True)
        copied = []
        for name in REGISTRY_FILES:
            source = os.path.join(storage_dir, name)
            if os.path.exists(source):
                task.executor(shutil.copy2, source, os.path.join(backup_dir, name))
                copied.append(name)
        result["backup_dir"] = backup_dir
        result["restore_with"] = _restore_hint(backup_dir)
        log.info(f"{CLEANUP_SERVICE}: backed up {', '.join(copied)} to {backup_dir}")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    for entry in live_entities:
        ent_reg.async_remove(entry.entity_id)
    for device in live_devices:
        dev_reg.async_remove_device(device.id)

    # async_remove turns each live entry into a tombstone of its own, and those
    # are orphans too, so the sweep runs against a freshly collected list.
    _live, dead_entities, _live_dev, dead_devices = _collect(
        domain, entity_filter, live_entry_ids
    )
    if chosen_entities:
        wanted = set(chosen_entities)
        dead_entities = [(k, e) for k, e in dead_entities if e.entity_id in wanted]
    if chosen_devices:
        wanted = set(chosen_devices)
        dead_devices = [(k, d) for k, d in dead_devices if d.id in wanted]

    # .pop() on these containers goes through __delitem__, which keeps the
    # registries' internal indexes consistent.
    dead_device_container = _deleted_device_container(dev_reg)
    for key, _entry in dead_entities:
        ent_reg.deleted_entities.pop(key, None)
    for key, _device in dead_devices:
        dead_device_container.pop(key, None)

    if live_entities or dead_entities:
        ent_reg.async_schedule_save()
    if live_devices or dead_devices:
        dev_reg.async_schedule_save()

    if purge_storage:
        for entity_id in result["restore_state_purged"]:
            restore_data.last_states.pop(entity_id, None)
        if result["restore_state_purged"]:
            # restore_state has no scheduled save; rewrite the file now.
            restore_data.async_dump_states()
        if storage_files:
            if backup:
                task.executor(os.makedirs, backup_dir, exist_ok=True)
            for name in storage_files:
                path = os.path.join(storage_dir, name)
                if backup:
                    # Moved, not copied: the backup directory is the only
                    # remaining copy of these files.
                    task.executor(shutil.move, path, os.path.join(backup_dir, name))
                else:
                    task.executor(os.remove, path)
            verb = "moved to backup" if backup else "deleted"
            log.info(f"{CLEANUP_SERVICE}: {verb} {', '.join(storage_files)}")

    if statistic_ids:
        _recorder_instance(hass).async_clear_statistics(statistic_ids)
        log.info(
            f"{CLEANUP_SERVICE}: cleared statistics for {len(statistic_ids)} entity(s)"
        )

    log.info(_summary(result, "APPLIED"))
    _notify(result)

    # What was just purged is no longer a candidate, and the checkboxes that
    # listed it are now stale.
    _scan(True)
    return result


def _restore_hint(backup_dir):
    """How to undo a run, given where its backup landed."""
    return (
        f"To undo: stop Home Assistant (`ha core stop`), copy every file from "
        f"{backup_dir} back into /config/.storage/, then `ha core start`. "
        "Do not copy them back while core is running -- the in-memory "
        "registries would overwrite them at the next flush."
    )


def _rescan_debounced():
    """Rescan once a burst of registry removals has settled.

    task.unique kills the rescan still waiting from the previous event, so
    deleting an integration -- which removes its entities one by one -- ends in
    a single scan a few seconds after the last removal.
    """
    task.unique(RESCAN_TASK)
    task.sleep(RESCAN_DEBOUNCE)
    result = _scan(True)
    if result["selector_changed"]:
        log.info(
            f"{SCAN_SERVICE}: registry removals settled; dropdown now offers "
            f"{len(result['candidates'])} domain(s)"
        )


# Removals only.  A removal is what leaves entries without an owner, and entity
# creation fires in bulk for every integration at startup -- triggering on it
# would spawn a task per entity for no gain.
@event_trigger("entity_registry_updated", "action == 'remove'")
def _rescan_after_entity_removal(**kwargs):
    """Deleting an integration should update the dropdown by itself."""
    _rescan_debounced()


@event_trigger("device_registry_updated", "action == 'remove'")
def _rescan_after_device_removal(**kwargs):
    """Same, for integrations whose devices outlive their entities."""
    _rescan_debounced()


@time_trigger("startup")
def _refresh_domain_dropdown_at_startup():
    """Build the dropdown once pyscript is up, and on every reload."""
    result = _scan(True)
    log.info(
        f"{SCAN_SERVICE}: dropdown built with {len(result['candidates'])} domain(s) "
        f"at startup (refreshed={result['selector_refreshed']})"
    )


def _counts(result):
    return [
        ("stale entities removed", len(result["entities_removed"])),
        ("deleted_entities purged", len(result["deleted_entities_purged"])),
        ("stale devices removed", len(result["devices_removed"])),
        ("deleted_devices purged", len(result["deleted_devices_purged"])),
        ("restore_state entries", len(result["restore_state_purged"])),
        (".storage files", len(result["storage_files_purged"])),
        ("statistics cleared", len(result["statistics_cleared"])),
    ]


def _summary(result, mode):
    lines = [f"{CLEANUP_SERVICE} [{mode}] domain={result['domain'] or '<unset>'}"]
    if result["filter"]:
        lines.append(f"  filter: {result['filter']}")
    for label, count in _counts(result):
        lines.append(f"  {label:<26} {count}")
    if result["aborted"]:
        lines.append(f"  ABORTED: {result['aborted']}")
    if result["statistics_note"]:
        lines.append(f"  {result['statistics_note']}")
    if result["backup_dir"]:
        lines.append(f"  backup: {result['backup_dir']}")
    if result["restore_with"]:
        lines.append(f"  {result['restore_with']}")
    return "\n".join(lines)


def _notify(result):
    mode = "Dry run" if result["dry_run"] else "Applied"
    if result["aborted"]:
        body = f"**Aborted.** {result['aborted']}"
    else:
        rows = "\n".join([f"| {label} | {count} |" for label, count in _counts(result)])
        body = f"| item | count |\n|---|---|\n{rows}\n"
        if result["filter"]:
            body = f"{body}\nFilter: `{result['filter']}`"
        if result["statistics_note"]:
            body = f"{body}\n\n{result['statistics_note']}"
        if result["backup_dir"]:
            body = f"{body}\nBackup: `{result['backup_dir']}`"
        if result["restore_with"]:
            body = f"{body}\n\n{result['restore_with']}"
        if result["dry_run"]:
            body = (
                f"{body}\n\nNothing changed. The entity and device checkboxes on "
                "the cleanup action now list exactly this, every box ticked; "
                "untick what you want to keep and run again with `dry_run: false`."
            )
    persistent_notification.create(
        title=f"{mode}: {result['domain'] or 'unknown'} registry cleanup",
        message=body,
        notification_id=f"{CLEANUP_SERVICE}_{result['domain'] or 'unset'}",
    )


def _notify_scan(result):
    found = result["domains"]
    if result["candidates"]:
        rows = "\n".join(
            [
                f"| `{domain}` | {found[domain]['deleted_entities']} |"
                f" {found[domain]['deleted_devices']} |"
                f" {found[domain]['live_entities']} |"
                f" {found[domain]['live_devices']} |"
                f" {found[domain]['storage_files']} |"
                for domain in result["candidates"]
            ]
        )
        body = (
            "Registry entries no config entry owns, biggest first:\n\n"
            "| domain | dead ent | dead dev | stale ent | stale dev | storage |\n"
            "|---|---|---|---|---|---|\n"
            f"{rows}\n"
        )
    else:
        body = (
            "Nothing needs cleaning: every registry entry still belongs to a "
            "configuration that exists.\n"
        )
    if result["selector_changed"]:
        body = (
            f"{body}\nThe domain dropdown on the cleanup action has been "
            "rebuilt; open pages pick it up on their own."
        )
    persistent_notification.create(
        title="Integration registry scan",
        message=body,
        notification_id=SCAN_SERVICE,
    )
