# IPFabric ⟹ Nautobot Integration — Developer Reference

> **Location:** `nautobot_ssot/integrations/ipfabric/`

---

## Table of Contents

1. [Overview & Data Flow](#1-overview--data-flow)
2. [File-by-File Reference](#2-file-by-file-reference)
   - [constants.py](#constantspy)
   - [signals.py](#signalspy)
   - [jobs.py](#jobspy)
   - [diffsync/adapters_shared.py](#diffsyncsadapters_sharedpy)
   - [diffsync/adapter_ipfabric.py](#diffsyncsadapter_ipfabricpy)
   - [diffsync/adapter_nautobot.py](#diffsyncsadapter_nautobotpy)
   - [diffsync/diffsync_models.py](#diffsyncsdffsync_modelspy)
   - [utilities/utils.py](#utilitiesutilspy)
   - [utilities/nbutils.py](#utilitiesnbutilspy)
3. [DiffSync Model Hierarchy](#3-diffsync-model-hierarchy)
4. [Job Execution Flow](#4-job-execution-flow)
5. [Probable Causes of the Pydantic Validation Error](#5-probable-causes-of-the-pydantic-validation-error)

---

## 1. Overview & Data Flow

The integration pulls network inventory data from **IP Fabric** (source) and syncs it into **Nautobot** (destination) using the [DiffSync](https://diffsync.readthedocs.io/) library.

```
IP Fabric API
    │
    ▼
IPFabricDiffSync (adapter_ipfabric.py)   ◄── loads sites, devices, stacks
    │
    │  diff_from()
    ▼
NautobotDiffSync (adapter_nautobot.py)   ◄── loads locations, devices from Nautobot
    │
    │  sync_from()  ← only executed on a REAL run (not dryrun)
    ▼
diffsync_models.py  create() / update() / delete()
    │
    ▼
Nautobot ORM (via nbutils.py helpers)
```

**Key concepts:**
- **Dryrun** – only `diff_from()` runs; no DB writes occur.
- **Real run** – `sync_from()` triggers `create()` / `update()` / `delete()` on each DiffSync model.
- **Safe Delete Mode** – instead of hard-deleting, objects are tagged with `SSoT Safe Delete` and their status is changed (e.g., `Offline`, `Decommissioning`).
- **Unique key for devices** – `serial_number` (mapped to the Nautobot `Device.serial` field). Devices with a missing or too-long serial number are skipped.

---

## 2. File-by-File Reference

### `constants.py`

Reads all configuration from `settings.PLUGINS_CONFIG["nautobot_ssot"]` and exposes them as module-level constants.

| Constant | Default | Description |
|---|---|---|
| `IPFABRIC_HOST` | — | IP Fabric base URL (required) |
| `IPFABRIC_API_TOKEN` | — | IP Fabric API token (required) |
| `IPFABRIC_SSL_VERIFY` | — | SSL verification flag |
| `NAUTOBOT_HOST` | — | Nautobot base URL |
| `IPFABRIC_TIMEOUT` | `15` | HTTP timeout in seconds |
| `DEFAULT_DEVICE_ROLE` | `"Network Device"` | Fallback role when IPFabric provides none |
| `DEFAULT_DEVICE_STATUS` | `"Active"` | Status assigned to newly created devices |
| `SAFE_DELETE_DEVICE_STATUS` | `"Offline"` | Status set on devices flagged for safe delete |
| `SAFE_DELETE_LOCATION_STATUS` | `"Decommissioning"` | Status set on locations flagged for safe delete |
| `SAFE_DELETE_VLAN_STATUS` | `"Deprecated"` | Status set on VLANs flagged for safe delete |
| `SAFE_DELETE_IPADDRESS_STATUS` | `"Deprecated"` | Status set on IPs flagged for safe delete |
| `LAST_SYNCHRONIZED_CF_NAME` | `"last_synced_from_sor"` | Custom field key updated on every synced object |
| `SYNC_IPF_DEV_TYPE_TO_ROLE` | `True` | Map IPFabric `dev_type` to Nautobot Role |
| `IP_FABRIC_USE_CANONICAL_INTERFACE_NAME` | `False` | Normalize interface names |

---

### `signals.py`

Runs once when Nautobot's database is ready (`nautobot_database_ready` signal).

**What it creates automatically:**

| Object Type | Name / Key |
|---|---|
| Tag | `SSoT Synced from IPFabric` |
| Tag | `SSoT Safe Delete` |
| LocationType | `Site` (with content types for Device, Prefix, VLAN) |
| CustomField | `system_of_record` (text) on all relevant models |
| CustomField | `last_synced_from_sor` (date) on all relevant models |
| CustomField | `ipfabric_site_id` (text) on Location |
| CustomField | `ipfabric_type` (text) on Role |

**Important:** If any of these objects or custom fields are missing at runtime, write operations in a real job run will fail with validation errors.

---

### `jobs.py`

Defines the `IpFabricDataSource` Nautobot Job class exposed to the UI.

**Class-level variables (job form fields):**

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `debug` | `BooleanVar` | — | Enable verbose logging |
| `safe_delete_mode` | `BooleanVar` | `True` | Soft-delete instead of hard-delete |
| `sync_ipfabric_tagged_only` | `BooleanVar` | `True` | Only sync objects tagged with `SSoT Synced from IPFabric` |
| `location_filter` | `OptionalObjectVar` | None | Scope sync to a single Nautobot Location |
| `snapshot` | `ChoiceVar` | `$last` | IP Fabric snapshot to use (built dynamically) |

**Key methods:**

- `_init_ipf_client()` – Creates an `IPFClient` from `constants.py` settings.
- `_get_vars()` – Extended to dynamically build the snapshot dropdown from IPFabric's loaded snapshots.
- `sync_data()` – The main orchestration method:
  1. Initialises the IPFabric adapter.
  2. Sets `safe_delete_mode` on model adapters.
  3. Initialises the Nautobot adapter.
  4. Runs `diff_from()` and saves the diff.
  5. If **not** a dryrun: calls `sync_from()` with `CONTINUE_ON_FAILURE`.

---

### `diffsync/adapters_shared.py`

Defines `DiffSyncModelAdapters`, a thin base `Adapter` that:
- Registers the four DiffSync model types: `location`, `device`, `interface`, `vlan`.
- Sets `top_level = ["location"]` so DiffSync traverses the tree starting from locations.
- Carries a class-level `safe_delete_mode: ClassVar[bool] = True`.

Both `IPFabricDiffSync` and `NautobotDiffSync` inherit from this class so they share the same model registry.

---

### `diffsync/adapter_ipfabric.py`

**Class:** `IPFabricDiffSync`

Loads data **from IP Fabric** into DiffSync model objects.

| Method | What it loads |
|---|---|
| `load_sites()` | Calls `client.inventory.sites.all()`, creates `Location` models |
| `load_data()` | Fetches VLANs, managed IPv4, interfaces, and stack info in bulk |
| `load()` | Calls `load_sites()` then iterates over each site's devices |

**Device loading logic (inside `load()`):**
1. For each device in IPFabric:
   - If the device has **no stack entry** → single `Device` model.
   - If the device **is part of a stack** → one `Device` per stack member with `vc_name`, `vc_master`, `vc_position`, `vc_priority` populated.
2. Devices with no serial number (or serial exceeding Nautobot's max length) are **skipped**.

**Helper function `pseudo_management_interface()`:**  
Creates a fake `pseudo_mgmt` interface if the device's primary management IP doesn't match any inventory interface (NAT scenario).

---

### `diffsync/adapter_nautobot.py`

**Class:** `NautobotDiffSync`

Loads data **from Nautobot** into DiffSync model objects for comparison.

| Method | What it loads |
|---|---|
| `load_data()` | Locations (filtered by tag/location filter), then devices per location |
| `load_device()` | Individual devices; skips any with empty `serial` field |
| `load_vlans()` | VLANs per location (not called in this codebase revision — VLANs excluded) |
| `load_interfaces()` | Interfaces per device (not called in current `load_data()`) |
| `get_initial_location()` | Applies `sync_ipfabric_tagged_only` and `location_filter` rules |
| `sync_complete()` | After sync: hard-deletes objects in `objects_to_delete` if safe_delete_mode is off |

**Critical note:** `load_data()` is decorated `@transaction.atomic` — if any exception escapes, the entire load transaction is rolled back.

---

### `diffsync/diffsync_models.py`

Contains the four DiffSync model classes, each owning `create()`, `update()`, and `delete()` methods that write to the Nautobot ORM.

#### `DiffSyncExtras` (base mixin)

- `safe_delete()` — Either adds the object to `objects_to_delete` (hard delete) or tags it with `SSoT Safe Delete` + changes its status.

#### `Location`

- **Identifiers:** `name`
- **Attributes:** `site_id`, `status`
- `create()` calls `nbutils.create_location()`.
- `update()` updates `ipfabric_site_id` custom field and status, then calls `tag_object()`.
- `delete()` calls `safe_delete()` with `SAFE_DELETE_LOCATION_STATUS`.

#### `Device`

- **Identifiers:** `serial_number`
- **Attributes:** `name`, `location_name`, `model`, `vendor`, `role`, `status`, `platform`, `vc_*`
- `create()`:  
  1. Gets or creates `DeviceType` → `Manufacturer`.
  2. Gets or creates `Platform`.
  3. Gets or creates `Role` (using `ipfabric_type` custom field lookup).
  4. Gets or creates `Status` (`Active`).
  5. Gets or creates `Location`.
  6. Calls `NautobotDevice.objects.get_or_create()` with `name + serial + status + device_type + role + location`.
  7. Tags the device and calls `validated_save()`.
  8. Optionally creates/updates `VirtualChassis`.
- `update()` updates `Device` fields by serial number lookup.
- `delete()` calls `safe_delete()` with `SAFE_DELETE_DEVICE_STATUS`.

#### `Interface`

- **Identifiers:** `name`, `device_name`
- **Attributes:** `description`, `enabled`, `mac_address`, `mtu`, `type`, `mgmt_only`, `ip_address`, `subnet_mask`, `ip_is_primary`, `status`
- `create()` creates interface on device (matched by name + IPFabric tag), creates IP, assigns primary IP.
- `update()` / `delete()` update/remove via device name + tag filter.

#### `Vlan`

- **Identifiers:** `name`, `location`
- **Attributes:** `vid`, `status`, `description`
- Standard create/update/delete against `VLAN` objects.

---

### `utilities/utils.py`

Provides `convert_media_type(media_type, interface_name)`.

Converts IP Fabric's raw media type strings (e.g., `SFP-10GBase-LR`) into Nautobot interface type choices (e.g., `10gbase-x-sfpp`). Falls back to regex-based name matching when `media_type` is `None`, and ultimately falls back to `DEFAULT_INTERFACE_TYPE`.

---

### `utilities/nbutils.py`

Low-level Nautobot ORM helper functions used by the DiffSync models.

| Function | Purpose |
|---|---|
| `create_location()` | Get or create a `Location` with type `Site` and attach `ipfabric_site_id` CF |
| `create_manufacturer()` | Get or create a `Manufacturer` |
| `create_device_type_object()` | Get or create a `DeviceType` under the correct manufacturer |
| `create_platform_object()` | Get or create a `Platform`; skips if it exists under a different manufacturer |
| `get_or_create_device_role_object()` | Looks up `Role` by `ipfabric_type` CF, creates if missing |
| `create_status()` | Get or create a `Status` with content type association |
| `create_ip()` | Get or create an `IPAddress`; auto-creates missing `Prefix` if needed |
| `create_interface()` | Get or create an `Interface` on a device |
| `create_vlan()` | Get or create a `VLAN` at a location |
| `tag_object()` | Adds `SSoT Synced from IPFabric` tag, sets `system_of_record = "IPFabric"`, sets `last_synced_from_sor` to today, and calls `validated_save()` |

---

## 3. DiffSync Model Hierarchy

```
Location
├── Device (0..n)
│   └── (Interface — defined in model but not loaded as children in current load())
└── Vlan (0..n)    [currently excluded from ipfabric adapter load()]
```

`top_level = ["location"]` — DiffSync walks the tree from Location downwards.

---

## 4. Job Execution Flow

```
IpFabricDataSource.run()
    └── sync_data()
            ├── 1. Init IPFabric client (IPFClient)
            ├── 2. Create IPFabricDiffSync adapter
            │         └── load()
            │               ├── load_sites()         → Location models
            │               └── load() loop          → Device models
            ├── 3. Set safe_delete_mode on adapters
            ├── 4. Create NautobotDiffSync adapter
            │         └── load()
            │               └── load_data()          → Location + Device models from Nautobot
            ├── 5. diff_from()                       → Compute diff (always runs)
            ├── 6. Save diff to sync object
            └── 7. [if NOT dryrun] sync_from()
                      ├── Device.create() / update() / delete()
                      ├── Location.create() / update() / delete()
                      └── sync_complete() → hard-delete queued objects
```

---

## 5. Probable Causes of the Pydantic Validation Error

The error occurs **only on real runs** (step 7 above), after the dryrun diff is computed correctly. The `sync_from()` call triggers `create()` / `update()` / `delete()` on DiffSync models, each of which eventually calls `nautobot_object.validated_save()` (via `tag_object()`). Pydantic validation errors in this context almost always originate from one of the following root causes:

---

### ⚠️ Cause 1 — Missing or wrong Status for a newly created object

**Where:** `Device.create()` → `NautobotDevice.objects.get_or_create(status=device_status_object, ...)`

**Why it fails on real runs only:** On a dryrun, Django ORM writes are never actually committed, so a missing Status object only causes a silent log warning. On a real run, the `get_or_create()` call reaches `validated_save()` which triggers Nautobot's pydantic validation.

**Common scenario:** The statuses `Active`, `Offline`, `Decommissioning`, `Deprecated` are expected to exist in Nautobot. If your Nautobot instance is missing any of these (e.g., the status `"Offline"` for `SAFE_DELETE_DEVICE_STATUS` or `"Decommissioning"` for `SAFE_DELETE_LOCATION_STATUS`), `validated_save()` will raise a `ValidationError` that surfaces as a pydantic error.

**How to verify:** Check that all statuses referenced in `constants.py` exist in Nautobot's admin under **Extras → Statuses**:
- `Active`
- `Offline`
- `Decommissioning`
- `Deprecated`

---

### ⚠️ Cause 2 — `tag_object()` called on an object whose ContentType does not support the tag or custom field

**Where:** `nbutils.tag_object()` → `nautobot_object.validated_save()`

**Why:** The `SSoT Synced from IPFabric` tag and the `last_synced_from_sor` / `system_of_record` custom fields are created by `signals.py` on startup. If the tag or custom field is not associated with the correct content type (e.g., `Device`, `Location`), calling `validated_save()` on those objects will raise a pydantic validation error.

**How to verify:** In Nautobot admin, inspect the `SSoT Synced from IPFabric` tag and the `last_synced_from_sor` custom field to confirm they are assigned to `dcim | device`, `dcim | location`, etc.

---

### ⚠️ Cause 3 — `Device.get_or_create()` receives a field value that fails Nautobot's model validators

**Where:** `Device.create()` → `NautobotDevice.objects.get_or_create(...)`

**Common triggers:**
- `serial` field exceeds the Nautobot max length (50 chars). The adapter truncates it, but only if `len(serial_number) < device_serial_max_length`. A serial that is exactly `max_length` chars long is **skipped**, which is correct — but if the truncation logic allowed an empty string through, `get_or_create` with `serial=""` could match multiple existing devices.
- `name` field is `None` — if `device.hostname` returns `None` in IPFabric, `attrs["name"]` will be `None` and `Device.name` (declared `str`, not `Optional[str]`) will fail pydantic validation when the DiffSync model is instantiated during `sync_from()`.
- `model` → `DeviceType.model` field may have vendor-specific characters that fail Nautobot slug validation.

---

### ⚠️ Cause 4 — `virtual_chassis` constraint during `Device.create()` or `Device.update()`

**Where:** `Device._get_or_create_virtual_chassis()` → `device.validated_save()`

Setting `vc_position` and `vc_priority` on a device assigned to a VirtualChassis triggers additional Nautobot validation (`vc_position` must be unique within the VC). If two stack members somehow share the same `member` field value in IPFabric, both will get the same `vc_position`, violating the uniqueness constraint and causing a pydantic validation error.

---

### ⚠️ Cause 5 — `Role` content type missing for `Device`

**Where:** `get_or_create_device_role_object()` → `role_obj.content_types.add(...)` → subsequent `validated_save()`

If a `Role` is looked up by `ipfabric_type` CF but the role doesn't have `dcim | device` in its `content_types`, assigning that role to a device via `_device.role = device_role_object` followed by `validated_save()` will fail pydantic validation with a message like _"Role is not applicable to Device"_.

---

### 🔍 How to Get the Exact Error

The errors are caught broadly by `except (DjangoBaseDBError, ValidationError)` and logged, but **pydantic errors bubble up differently**. To pinpoint the exact field:

1. **Enable Debug mode** in the job run form — this activates `logger.debug(error)` in `Device.create()` (line 338 of `diffsync_models.py`) and logs the full exception.
2. Look for log lines containing `"Unable to perform a validated_save()"` or `"Unable to create a new Device"` immediately before the pydantic error.
3. If the error is not caught, it will appear in the job result as an unhandled exception with a full traceback identifying the field.

---

### ✅ Recommended Checks

1. Confirm all required Statuses exist in Nautobot (`Active`, `Offline`, `Decommissioning`, `Deprecated`).
2. Confirm the `SSoT Synced from IPFabric` tag is assigned to content types: `dcim | device`, `dcim | location`, `dcim | interface`, `ipam | ipaddress`, `ipam | vlan`, `dcim | devicetype`, `dcim | manufacturer`.
3. Confirm the custom fields `last_synced_from_sor`, `system_of_record`, `ipfabric_site_id`, `ipfabric_type` exist and are assigned to the same content types.
4. Run the job with **Debug enabled** to expose the full pydantic error details.
5. Check if any device hostnames returned by IPFabric are `None` or empty.
