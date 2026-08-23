"""Purge every registry trace of a deleted integration.

Removing an integration does not erase its registry history.  Entities become
tombstones in ``core.entity_registry`` -> ``deleted_entities`` and devices
become tombstones in ``core.device_registry`` -> ``deleted_devices``.  Home
Assistant keeps them for ORPHANED_ENTITY_KEEP_SECONDS (30 days), and when an
entity with a matching (domain, platform, unique_id) reappears,
``async_get_or_create`` restores the tombstone's entity_id, name, icon, aliases,
area and options onto the new entity.  A reinstall therefore inherits the old
naming scheme no matter what the new config entry asks for.

Two services are registered.

pyscript.integration_registry_scan   (read-only)

  Buckets every registry entry -- live and tombstoned, entities and devices --
  by the integration that owns it, and rebuilds the ``domain`` dropdown on the
  cleanup action below from what it finds.  Runs at startup (so every pyscript
  reload refreshes the list) and on demand.

  refresh_selector  rebuild the cleanup action's dropdown  (default: true)
  notification      raise a persistent notification        (default: true)

pyscript.integration_registry_cleanup

  Deletes those registry entries so the next install starts clean.

  domain         integration domain to purge              (required)
  dry_run        report only, change nothing              (default: true)
  purge_storage  also remove the integration's .storage
                 files and its core.restore_state entries (default: false)
  backup         copy the registries aside first          (default: true)

With ``backup`` on, everything the cleanup touches is copied to

    /config/registry_backups/<domain>_<YYYYmmdd_HHMMSS>/

as a flat directory: core.entity_registry, core.device_registry and
core.restore_state are copied there, and any purged .storage files are *moved*
there rather than deleted.  To undo a run, stop Home Assistant, copy every file
from that directory back into /config/.storage/, and start it again -- the
registries must not be written while core is running, or the in-memory copy
will simply overwrite them at the next flush.

For the same reason the registries are edited in memory through the registry
APIs here, never by writing .storage directly.
"""

import copy
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

# The free-text selector and description the cleanup action declares in its own
# docstring, captured the first time the dropdown is built so they can be put
# back when a scan finds something to offer.  Reset when pyscript reloads this
# file.
_ORIGINAL_DOMAIN_SELECTOR = None
_ORIGINAL_DOMAIN_DESCRIPTION = None


def _device_container(dev_reg):
    """Live device container.

    `DeviceRegistry.devices` became a deprecation-reporting view in 2026.9, so
    prefer the private attribute and fall back for older cores.
    """
    return getattr(dev_reg, "_devices", None) or dev_reg.devices


def _deleted_device_container(dev_reg):
    """Tombstoned device container, same deprecation story as above."""
    return getattr(dev_reg, "_deleted_devices", None) or dev_reg.deleted_devices


def _device_domains(device):
    """Every integration domain a device (live or tombstoned) belongs to."""
    domains = set()
    for identifier in device.identifiers:
        if len(identifier):
            domains.add(identifier[0])
    # DeletedDeviceEntry carries an explicit domain; DeviceEntry has no such
    # attribute, hence the getattr.
    explicit = getattr(device, "domain", None)
    if explicit:
        domains.add(explicit)
    return domains


def _device_matches(device, domain):
    """True if a device belongs to this integration."""
    return domain in _device_domains(device)


def _storage_names_for(names, storage_dir, domain):
    """.storage file names owned by this integration.

    Same rule the cleanup uses: the domain as a filename prefix, minus anything
    Home Assistant or HACS owns.
    """
    return sorted(
        [
            name
            for name in names
            if name.startswith(domain)
            and not name.startswith(STORAGE_KEEP)
            and os.path.isfile(os.path.join(storage_dir, name))
        ]
    )


def _restore_hint(backup_dir):
    """How to undo a run, given where its backup landed."""
    return (
        f"To undo: stop Home Assistant (`ha core stop`), copy every file from "
        f"{backup_dir} back into /config/.storage/, then `ha core start`. "
        "Do not copy them back while core is running -- the in-memory "
        "registries would overwrite them at the next flush."
    )


# Blocking file work is handed to task.executor one stdlib call at a time:
# task.executor only accepts real Python functions, never pyscript-defined ones.


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


# Tombstone counts lead: they are what makes a reinstall inherit old names.
LABEL_PARTS = (
    ("deleted_entities", "entity tombstone", "entity tombstones"),
    ("deleted_devices", "device tombstone", "device tombstones"),
    ("live_entities", "live entity", "live entities"),
    ("live_devices", "live device", "live devices"),
    ("storage_files", "storage file", "storage files"),
)


def _has_tombstones(counts):
    """Whether a domain is worth offering for cleanup.

    A domain with live entries but no tombstones is simply an integration that
    works -- `person` and the other YAML-configured core integrations land
    there.  Offering those would invite someone to purge a running integration.
    """
    return bool(counts["deleted_entities"] or counts["deleted_devices"])


def _label(domain, counts, tombstones_only=False):
    """Dropdown label: the domain plus what is actually left behind.

    `tombstones_only` trims the label to the counts a sentence about tombstones
    cares about, so naming a blocked domain does not drag its live entity count
    along with it.
    """
    parts = []
    for key, one, many in LABEL_PARTS:
        if tombstones_only and not key.startswith("deleted_"):
            continue
        count = counts[key]
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    return f"{domain} - {', '.join(parts)}"


def _empty_dropdown_help(blocked):
    """What the `domain` field says when there is no dropdown to show.

    An empty text box under a description that talks about a dropdown reads as
    a bug, so the field explains what it is waiting for -- and names the
    domains that would qualify but for a config entry.
    """
    lines = [
        "Integration domain to purge, as it appears in its manifest.",
        "Nothing needs cleaning: an integration you deleted is listed here only"
        " if it left registry tombstones behind, and none has.",
    ]
    if blocked:
        lines.append(
            "Tombstones do exist for these, but their integration is still"
            f" configured, so they are not offered: {'; '.join(blocked)}."
        )
        lines.append(
            "Delete one under Settings > Devices & Services to clean it; the"
            " list rebuilds itself."
        )
    lines.append(
        "Home Assistant also drops tombstones by itself 30 days after the"
        " entity was removed, which empties this list on its own."
    )
    return " ".join(lines)


def _refresh_domain_selector(options, blocked):
    """Swap the cleanup action's `domain` field to a dropdown of `options`.

    This is the same call pyscript makes to publish a service's docstring YAML,
    so re-registering the description is a supported operation rather than a
    poke at internals.  The description already registered is read back and
    edited, which keeps the docstring the single source of truth.
    """
    global _ORIGINAL_DOMAIN_SELECTOR
    global _ORIGINAL_DOMAIN_DESCRIPTION

    published = async_get_cached_service_description(
        hass, PYSCRIPT_DOMAIN, CLEANUP_SERVICE
    )
    if not published:
        log.warning(
            f"{SCAN_SERVICE}: {PYSCRIPT_DOMAIN}.{CLEANUP_SERVICE} has no published "
            "description yet; leaving the domain field alone"
        )
        return False, False

    description = copy.deepcopy(published)
    field = description.get("fields", {}).get("domain")
    if field is None:
        log.warning(f"{SCAN_SERVICE}: {CLEANUP_SERVICE} has no 'domain' field")
        return False, False

    if _ORIGINAL_DOMAIN_SELECTOR is None:
        _ORIGINAL_DOMAIN_SELECTOR = copy.deepcopy(field.get("selector"))
        _ORIGINAL_DOMAIN_DESCRIPTION = field.get("description")

    if options:
        # custom_value does double duty: it keeps the field usable for a domain
        # the scan cannot see (e.g. one whose integration is still configured),
        # and it is what makes the field searchable -- ha-selector-select only
        # renders the filtering ha-generic-picker when custom_value is set,
        # falling back to a plain unfiltered ha-select otherwise.  Options stay
        # in scan order (most leftovers first), so no `sort` here.
        field["selector"] = {
            "select": {
                "mode": "dropdown",
                "custom_value": True,
                "options": options,
            }
        }
        field["description"] = _ORIGINAL_DOMAIN_DESCRIPTION
    else:
        field["selector"] = copy.deepcopy(_ORIGINAL_DOMAIN_SELECTOR)
        field["description"] = _empty_dropdown_help(blocked)

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


def _scan(refresh_selector):
    """Bucket every registry entry by owning integration."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    storage_dir = hass.config.path(".storage")
    storage_names = task.executor(os.listdir, storage_dir)

    found = {}

    for entry in list(ent_reg.entities.values()):
        _bucket(found, entry.platform)["live_entities"] += 1

    for entry in list(ent_reg.deleted_entities.values()):
        platform = getattr(entry, "platform", None)
        if platform:
            _bucket(found, platform)["deleted_entities"] += 1

    for device in list(_device_container(dev_reg).values()):
        for domain in _device_domains(device):
            _bucket(found, domain)["live_devices"] += 1

    for device in list(_deleted_device_container(dev_reg).values()):
        for domain in _device_domains(device):
            _bucket(found, domain)["deleted_devices"] += 1

    for domain in list(found):
        found[domain]["storage_files"] = len(
            _storage_names_for(storage_names, storage_dir, domain)
        )

    # Domains whose integration is still configured: the cleanup action refuses
    # to run on them, so they are not offered in the dropdown.  Matches the
    # abort check exactly, disabled and ignored entries included.
    configured = set()
    for entry in hass.config_entries.async_entries():
        configured.add(entry.domain)

    ranked = []
    skipped = []
    for domain in found:
        if not _has_tombstones(found[domain]):
            continue
        if domain in configured:
            skipped.append(domain)
            continue
        # Negative total sorts the biggest offender first, then by name.
        ranked.append((-sum(found[domain].values()), domain))

    options = [
        {"value": domain, "label": _label(domain, found[domain])}
        for _rank, domain in sorted(ranked)
    ]

    result = {
        "domains": found,
        "candidates": [option["value"] for option in options],
        "skipped_configured": sorted(skipped),
        "selector_refreshed": False,
        "selector_changed": False,
    }
    if refresh_selector:
        blocked = [
            _label(domain, found[domain], tombstones_only=True)
            for domain in sorted(skipped)
        ]
        refreshed, changed = _refresh_domain_selector(options, blocked)
        result["selector_refreshed"] = refreshed
        result["selector_changed"] = changed
    return result


@service(supports_response="optional")
def integration_registry_scan(refresh_selector=True, notification=True):
    """yaml
name: Integration registry scan
description: >-
  List every integration domain that still owns entity or device registry
  entries, live or tombstoned, and refresh the domain dropdown on the
  integration registry cleanup action.
fields:
  refresh_selector:
    description: >-
      Rebuild the domain dropdown on pyscript.integration_registry_cleanup from
      what this scan finds. An already-open browser tab keeps the old options
      until the page is reloaded.
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
        f"{SCAN_SERVICE}: {len(result['domains'])} domain(s) with registry entries, "
        f"{len(result['candidates'])} offered, "
        f"{len(result['skipped_configured'])} still configured, "
        f"selector_refreshed={result['selector_refreshed']}"
    )
    if notification:
        _notify_scan(result)
    return result


@service(supports_response="optional")
def integration_registry_cleanup(domain=None, dry_run=True, purge_storage=False, backup=True):
    """yaml
name: Integration registry cleanup
description: >-
  Remove every entity and device registry entry for an integration, including
  the deleted_entities / deleted_devices tombstones that make a reinstall
  inherit its old entity_ids and names.
fields:
  domain:
    description: >-
      Integration domain to purge, as it appears in its manifest. The dropdown
      is built by pyscript.integration_registry_scan and lists what each domain
      has left behind, most first; type to filter it. Run that action to
      refresh the list.
    example: span_panel
    required: true
    selector:
      text:
  dry_run:
    description: Report what would be removed without changing anything.
    default: true
    selector:
      boolean:
  purge_storage:
    description: >-
      Also remove the integration's own .storage files and its entries in
      core.restore_state.
    default: false
    selector:
      boolean:
  backup:
    description: >-
      Copy core.entity_registry, core.device_registry and core.restore_state to
      /config/registry_backups/<domain>_<timestamp>/ before changing anything;
      purged .storage files are moved there instead of being deleted. To undo a
      run, stop Home Assistant, copy the files back into /config/.storage/ and
      start it again. With this off, purged .storage files are deleted outright
      and the run cannot be undone.
    default: true
    selector:
      boolean:
"""
    domain = str(domain or "").strip()
    dry_run = bool(dry_run)
    purge_storage = bool(purge_storage)
    backup = bool(backup)

    storage_dir = hass.config.path(".storage")
    backup_dir = hass.config.path(
        BACKUP_ROOT, f"{domain}_{time.strftime('%Y%m%d_%H%M%S')}"
    )

    result = {
        "domain": domain,
        "dry_run": dry_run,
        "aborted": None,
        "backup_dir": None,
        "restore_with": None,
        "entities_removed": [],
        "deleted_entities_purged": [],
        "devices_removed": [],
        "deleted_devices_purged": [],
        "restore_state_purged": [],
        "storage_files_purged": [],
    }

    if not domain:
        result["aborted"] = "No domain given. Pass the integration domain to purge."
        log.error(f"{CLEANUP_SERVICE}: {result['aborted']}")
        _notify(result)
        return result

    # A live install must be deleted through the UI first, otherwise the
    # integration would simply rewrite everything this service removes.
    entries = hass.config_entries.async_entries(domain)
    if entries:
        result["aborted"] = (
            f"{len(entries)} config entry(s) for '{domain}' still exist. "
            "Delete the integration under Settings > Devices & Services first."
        )
        log.error(f"{CLEANUP_SERVICE}: {result['aborted']}")
        _notify(result)
        return result

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    live_devices_container = _device_container(dev_reg)
    dead_devices_container = _deleted_device_container(dev_reg)

    # Live entities first: async_remove() turns each one into a tombstone, so
    # the tombstone sweep below has to run after this.
    live_entities = [
        entry for entry in list(ent_reg.entities.values()) if entry.platform == domain
    ]
    result["entities_removed"] = sorted([entry.entity_id for entry in live_entities])
    if not dry_run:
        for entry in live_entities:
            ent_reg.async_remove(entry.entity_id)

    # Live devices, same ordering rule.
    live_devices = [
        device
        for device in list(live_devices_container.values())
        if _device_matches(device, domain)
    ]
    result["devices_removed"] = sorted(
        [f"{device.name or '?'} ({device.id})" for device in live_devices]
    )
    if not dry_run:
        for device in live_devices:
            dev_reg.async_remove_device(device.id)

    # Tombstones -- the entries that actually cause entity_id and name reuse.
    dead_entities = [
        (key, entry)
        for key, entry in list(ent_reg.deleted_entities.items())
        if getattr(entry, "platform", None) == domain
    ]
    result["deleted_entities_purged"] = sorted(
        [f"{entry.entity_id}  <-  {entry.unique_id}" for _key, entry in dead_entities]
    )

    dead_devices = [
        (key, device)
        for key, device in list(dead_devices_container.items())
        if _device_matches(device, domain)
    ]
    result["deleted_devices_purged"] = sorted(
        [f"{sorted(device.identifiers)} ({device.id})" for _key, device in dead_devices]
    )

    # Every entity_id this integration ever owned, used to prune restore_state.
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
        matches = task.executor(glob.glob, os.path.join(storage_dir, domain + "*"))
        storage_files = sorted(
            [
                path
                for path in matches
                if os.path.isfile(path)
                and not os.path.basename(path).startswith(STORAGE_KEEP)
            ]
        )
        result["storage_files_purged"] = [os.path.basename(p) for p in storage_files]

    if dry_run:
        if backup:
            result["backup_dir"] = f"{backup_dir}  (would be created)"
            result["restore_with"] = _restore_hint("that directory")
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

    # .pop() on these containers goes through __delitem__, which keeps the
    # registries' internal indexes consistent.
    for key, _entry in dead_entities:
        ent_reg.deleted_entities.pop(key, None)
    for key, _device in dead_devices:
        dead_devices_container.pop(key, None)

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
            for path in storage_files:
                if backup:
                    # Moved, not copied: the backup directory is the only
                    # remaining copy of these files.
                    task.executor(
                        shutil.move, path, os.path.join(backup_dir, os.path.basename(path))
                    )
                else:
                    task.executor(os.remove, path)
            verb = "moved to backup" if backup else "deleted"
            log.info(
                f"{CLEANUP_SERVICE}: {verb} {', '.join(result['storage_files_purged'])}"
            )

    log.info(_summary(result, "APPLIED"))
    _notify(result)

    # What was just purged is no longer a candidate.
    _scan(True)
    return result


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


# Removals only.  A removal is what creates tombstones, and entity creation
# fires in bulk for every integration at startup -- triggering on it would spawn
# a task per entity for no gain.  A domain that becomes configured again is
# caught by the next scan, and until then picking it merely aborts with
# "config entry still exists".
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
        ("live entities removed", len(result["entities_removed"])),
        ("deleted_entities purged", len(result["deleted_entities_purged"])),
        ("live devices removed", len(result["devices_removed"])),
        ("deleted_devices purged", len(result["deleted_devices_purged"])),
        ("restore_state entries", len(result["restore_state_purged"])),
        (".storage files", len(result["storage_files_purged"])),
    ]


def _summary(result, mode):
    lines = [f"{CLEANUP_SERVICE} [{mode}] domain={result['domain'] or '<unset>'}"]
    for label, count in _counts(result):
        lines.append(f"  {label:<26} {count}")
    if result["aborted"]:
        lines.append(f"  ABORTED: {result['aborted']}")
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
        if result["backup_dir"]:
            body = f"{body}\nBackup: `{result['backup_dir']}`"
        if result["restore_with"]:
            body = f"{body}\n\n{result['restore_with']}"
        if result["dry_run"]:
            body = f"{body}\n\nNothing changed. Re-run with `dry_run: false` to apply."
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
                f"| `{domain}` | {found[domain]['live_entities']} |"
                f" {found[domain]['deleted_entities']} |"
                f" {found[domain]['live_devices']} |"
                f" {found[domain]['deleted_devices']} |"
                f" {found[domain]['storage_files']} |"
                for domain in result["candidates"]
            ]
        )
        body = (
            "Domains with registry tombstones and no config entry, "
            "biggest first:\n\n"
            "| domain | live ent | dead ent | live dev | dead dev | storage |\n"
            "|---|---|---|---|---|---|\n"
            f"{rows}\n"
        )
    else:
        body = (
            "Nothing needs cleaning: no deleted integration has left registry "
            "tombstones behind, so the cleanup action keeps a plain text "
            "field.\n"
        )

    quiet = len([d for d in found if not _has_tombstones(found[d])])
    if quiet:
        body = (
            f"{body}\n{quiet} other domain(s) own registry entries but no "
            "tombstones, so nothing about them needs cleaning.\n"
        )

    if result["skipped_configured"]:
        skipped = ", ".join([f"`{d}`" for d in result["skipped_configured"]])
        body = (
            f"{body}\nStill configured, so not offered: {skipped}. Delete the "
            "integration first to clean one of these.\n"
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
